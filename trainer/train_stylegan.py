import math
import shutil
from pathlib import Path

import torch
import hydra
import pytorch_lightning as pl
from pytorch_lightning.utilities import rank_zero_only
from torch_ema import ExponentialMovingAverage
from torchvision.utils import save_image
from cleanfid import fid

from dataset.mesh import FaceGraphMeshDataset, to_vertex_colors_scatter, GraphDataLoader, to_device_graph_data, to_device
from model.augment import AugmentPipe
from model.differentiable_renderer import DifferentiableRenderer
from model.generator import Generator
from model.discriminator import Discriminator
from model.loss import PathLengthPenalty, compute_gradient_penalty
from trainer import create_trainer
from util.timer import Timer

torch.backends.cudnn.benchmark = True
torch.backends.cudnn.allow_tf32 = True
torch.backends.cudnn.deterministic = False


class StyleGAN2Trainer(pl.LightningModule):

    def __init__(self, config):
        super().__init__()
        self.save_hyperparameters(config)
        self.config = config
        self.G = Generator(config.latent_dim, config.latent_dim, config.num_mapping_layers, config.image_size, 3)
        self.D = Discriminator(config.image_size, 3)
        self.R = None
        self.augment_pipe = AugmentPipe(config.ada_start_p, config.ada_target, config.ada_interval, config.ada_fixed, config.batch_size)
        # print_module_summary(self.G, (torch.zeros(self.config.batch_size, self.config.latent_dim), ))
        # print_module_summary(self.D, (torch.zeros(self.config.batch_size, 3, config.image_size, config.image_size), ))
        self.train_set = FaceGraphMeshDataset(config)
        self.val_set = FaceGraphMeshDataset(config, config.num_eval_images)
        self.grid_z = torch.randn(config.num_eval_images, self.config.latent_dim)
        self.eval_graph_data = next(iter(GraphDataLoader(self.train_set, batch_size=config.batch_size)))['graph_data']
        self.automatic_optimization = False
        self.path_length_penalty = PathLengthPenalty(0.01, 2)
        self.ema = None
        level_mask = torch.tensor(list(range(self.config.batch_size))).long()
        level_mask = level_mask.unsqueeze(-1).expand(-1, config.num_faces[0]).reshape(-1)
        self.register_buffer("face_color_idx", torch.tensor(self.train_set.indices_src * self.config.batch_size).long() + level_mask * len(self.train_set.indices_src))
        self.register_buffer("face_batch_idx", level_mask)
        self.register_buffer("indices_img_i", torch.tensor(self.train_set.indices_dest_i * self.config.batch_size).long())
        self.register_buffer("indices_img_j", torch.tensor(self.train_set.indices_dest_i * self.config.batch_size).long())

    def configure_optimizers(self):
        g_opt = torch.optim.Adam(list(self.G.parameters()), lr=self.config.lr_g, betas=(0.0, 0.99), eps=1e-8)
        d_opt = torch.optim.Adam(self.D.parameters(), lr=self.config.lr_d, betas=(0.0, 0.99), eps=1e-8)
        return g_opt, d_opt

    def forward(self, limit_batch_size=False):
        z = self.latent(limit_batch_size)
        w = self.get_mapped_latent(z, 0.9)
        fake = self.G.synthesis(w)
        return fake, w

    def g_step(self, batch):
        g_opt = self.optimizers()[0]
        g_opt.zero_grad(set_to_none=True)
        fake, w = self.forward()
        p_fake = self.D(self.augment_pipe(self.render(fake, batch)))
        gen_loss = torch.nn.functional.softplus(-p_fake).mean()
        gen_loss.backward()
        log_gen_loss = gen_loss.item()
        g_opt.step()
        self.log("G", log_gen_loss, on_step=True, on_epoch=False, prog_bar=True, logger=True, sync_dist=True)

    def g_regularizer(self, batch):
        g_opt = self.optimizers()[0]
        g_opt.zero_grad(set_to_none=True)
        fake, w = self.forward()
        plp = self.path_length_penalty(self.render(fake, batch), w)
        if not torch.isnan(plp):
            gen_loss = self.config.lambda_plp * plp * self.config.lazy_path_penalty_interval
            self.log("rPLP", plp, on_step=True, on_epoch=False, prog_bar=False, logger=True, sync_dist=True)
            gen_loss.backward()
            g_opt.step()

    def d_step(self, batch):
        d_opt = self.optimizers()[1]
        d_opt.zero_grad(set_to_none=True)

        fake, _ = self.forward()
        p_fake = self.D(self.augment_pipe(self.render(fake.detach(), batch)))
        fake_loss = torch.nn.functional.softplus(p_fake).mean()
        fake_loss.backward()

        p_real = self.D(self.augment_pipe(self.render(batch["y"], batch, convert_to_face=False)))
        self.augment_pipe.accumulate_real_sign(p_real.sign().detach())

        # Get discriminator loss
        real_loss = torch.nn.functional.softplus(-p_real).mean()
        real_loss.backward()

        d_opt.step()

        self.log("D_real", real_loss, on_step=True, on_epoch=False, prog_bar=False, logger=True, sync_dist=True)
        self.log("D_fake", fake_loss, on_step=True, on_epoch=False, prog_bar=False, logger=True, sync_dist=True)
        disc_loss = real_loss + fake_loss
        self.log("D", disc_loss, on_step=True, on_epoch=False, prog_bar=True, logger=True, sync_dist=True)

    def d_regularizer(self, batch):
        d_opt = self.optimizers()[1]
        d_opt.zero_grad(set_to_none=True)
        image = self.render(batch["y"], batch, convert_to_face=False)
        image.requires_grad_()
        p_real = self.D(self.augment_pipe(image, True))
        gp = compute_gradient_penalty(image, p_real)
        disc_loss = self.config.lambda_gp * gp * self.config.lazy_gradient_penalty_interval
        disc_loss.backward()
        d_opt.step()
        self.log("rGP", gp, on_step=True, on_epoch=False, prog_bar=False, logger=True, sync_dist=True)

    def render(self, texture, batch, convert_to_face=True):
        if convert_to_face:
            face_colors = FaceGraphMeshDataset.to_face_colors(texture, self.face_color_idx, self.face_batch_idx, self.indices_img_i, self.indices_img_j)
        else:
            face_colors = texture
        rendered_color = self.R.render(batch['vertices'], batch['indices'], to_vertex_colors_scatter(face_colors, batch), batch["ranges"].cpu())
        return rendered_color.permute((0, 3, 1, 2))

    def training_step(self, batch, batch_idx):
        # optimize generator
        self.g_step(batch)

        if self.global_step > self.config.lazy_path_penalty_after and (self.global_step + 1) % self.config.lazy_path_penalty_interval == 0:
            self.g_regularizer(batch)

        # torch.nn.utils.clip_grad_norm_(self.G.parameters(), max_norm=1.0)

        self.ema.update(self.G.parameters())

        # optimize discriminator

        self.d_step(batch)

        if (self.global_step + 1) % self.config.lazy_gradient_penalty_interval == 0:
            self.d_regularizer(batch)

        self.execute_ada_heuristics()

    def execute_ada_heuristics(self):
        if (self.global_step + 1) % self.config.ada_interval == 0:
            self.augment_pipe.heuristic_update()
        self.log("aug_p", self.augment_pipe.p.item(), on_step=True, on_epoch=False, prog_bar=False, logger=True, sync_dist=True)

    def validation_step(self, batch, batch_idx):
        pass

    @rank_zero_only
    def validation_epoch_end(self, _val_step_outputs):

        with Timer("export_textures"):
            odir_real, odir_fake, odir_samples, odir_textures = self.create_directories()
            self.export_textures("", odir_textures, None)
            self.ema.store(self.G.parameters())
            self.ema.copy_to([p for p in self.G.parameters() if p.requires_grad])
            self.export_textures("ema_", odir_textures, odir_fake)
            self.ema.restore([p for p in self.G.parameters() if p.requires_grad])
            latents = self.grid_z.split(self.config.batch_size)

        with Timer("export_samples"):
            for iter_idx, batch in enumerate(self.val_dataloader()):
                batch = to_device(batch, self.device)
                real_render = self.render(batch['y'], batch, convert_to_face=False).cpu()
                fake_render = self.render(self.G(latents[iter_idx].to(self.device), noise_mode='const'), batch).cpu()
                save_image(real_render, odir_samples / f"real_{iter_idx}.jpg", value_range=(-1, 1), normalize=True)
                save_image(fake_render, odir_samples / f"fake_{iter_idx}.jpg", value_range=(-1, 1), normalize=True)
                texture = self.get_face_colors_as_texture_maps(batch['y']).cpu()
                for batch_idx in range(texture.shape[0]):
                    save_image(texture[batch_idx], odir_real / f"{iter_idx}_{batch_idx}.jpg", value_range=(-1, 1), normalize=True)
        fid_score = fid.compute_fid(odir_real, odir_fake, device=self.device)
        kid_score = fid.compute_kid(odir_real, odir_fake, device=self.device)
        self.log(f"fid", fid_score, on_step=False, on_epoch=True, prog_bar=False, logger=True, rank_zero_only=True, sync_dist=True)
        self.log(f"kid", kid_score, on_step=False, on_epoch=True, prog_bar=False, logger=True, rank_zero_only=True, sync_dist=True)
        print(f'FID: {fid_score:.3f} , KID: {kid_score:.3f}')
        shutil.rmtree(odir_real.parent)

    def get_mapped_latent(self, z, style_mixing_prob):
        if torch.rand(()).item() < style_mixing_prob:
            cross_over_point = int(torch.rand(()).item() * self.G.mapping.num_ws)
            w1 = self.G.mapping(z[0])[:, :cross_over_point, :]
            w2 = self.G.mapping(z[1], skip_w_avg_update=True)[:, cross_over_point:, :]
            return torch.cat((w1, w2), dim=1)
        else:
            w = self.G.mapping(z[0])
            return w

    def latent(self, limit_batch_size=False):
        batch_size = self.config.batch_size if not limit_batch_size else self.config.batch_size // self.path_length_penalty.pl_batch_shrink
        z1 = torch.randn(batch_size, self.config.latent_dim).to(self.device)
        z2 = torch.randn(batch_size, self.config.latent_dim).to(self.device)
        return z1, z2

    def train_dataloader(self):
        return GraphDataLoader(self.train_set, self.config.batch_size, shuffle=True, pin_memory=True, drop_last=True, num_workers=self.config.num_workers)

    def val_dataloader(self):
        return GraphDataLoader(self.val_set, self.config.batch_size, shuffle=True, drop_last=True, num_workers=self.config.num_workers)

    def export_textures(self, prefix, output_dir_vis, output_dir_fid):
        vis_generated_images = []
        for iter_idx, latent in enumerate(self.grid_z.split(self.config.batch_size)):
            latent = latent.to(self.device)
            fake = self.G(latent, noise_mode='const')
            fake_texture = fake.cpu()
            if output_dir_fid is not None:
                for batch_idx in range(fake_texture.shape[0]):
                    save_image(fake_texture[batch_idx], output_dir_fid / f"{iter_idx}_{batch_idx}.jpg", value_range=(-1, 1), normalize=True)
            if iter_idx < self.config.num_vis_images // self.config.batch_size:
                vis_generated_images.append(fake_texture)
        torch.cuda.empty_cache()
        vis_generated_images = torch.cat(vis_generated_images, dim=0)
        save_image(vis_generated_images, output_dir_vis / f"{prefix}{self.global_step:06d}.png", nrow=int(math.sqrt(vis_generated_images.shape[0])), value_range=(-1, 1), normalize=True)

    def create_directories(self):
        output_dir_fid_real = Path(f'runs/{self.config.experiment}/fid/real')
        output_dir_fid_fake = Path(f'runs/{self.config.experiment}/fid/fake')
        output_dir_samples = Path(f'runs/{self.config.experiment}/images/')
        output_dir_textures = Path(f'runs/{self.config.experiment}/textures/')
        for odir in [output_dir_fid_real, output_dir_fid_fake, output_dir_samples, output_dir_textures]:
            odir.mkdir(exist_ok=True, parents=True)
        return output_dir_fid_real, output_dir_fid_fake, output_dir_samples, output_dir_textures

    def get_face_colors_as_texture_maps(self, face_colors):
        level_mask = self.get_level_mask(face_colors)
        face_colors_as_texture_map = self.val_set.to_image(face_colors, level_mask)
        return face_colors_as_texture_map

    def get_level_mask(self, face_colors):
        level_mask = torch.tensor(list(range(self.config.batch_size))).long().to(face_colors.device)
        level_mask = level_mask.unsqueeze(-1).expand(-1, face_colors.shape[0] // self.config.batch_size).reshape(-1)
        return level_mask

    def on_train_start(self):
        if self.ema is None:
            self.ema = ExponentialMovingAverage(self.G.parameters(), 0.995)
        if self.R is None:
            self.R = DifferentiableRenderer(self.config.image_size)

    def on_validation_start(self):
        if self.ema is None:
            self.ema = ExponentialMovingAverage(self.G.parameters(), 0.995)
        if self.R is None:
            self.R = DifferentiableRenderer(self.config.image_size)


@hydra.main(config_path='../config', config_name='stylegan2')
def main(config):
    trainer = create_trainer("StyleGAN23D", config)
    model = StyleGAN2Trainer(config)
    trainer.fit(model)


if __name__ == '__main__':
    main()
