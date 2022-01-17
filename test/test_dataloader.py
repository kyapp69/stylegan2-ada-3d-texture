from pathlib import Path

import hydra
from torchvision.utils import save_image
from tqdm import tqdm
import torch

from dataset.mesh_real_features import FaceGraphMeshDataset
from dataset import GraphDataLoader, to_device, to_vertex_colors_scatter
from model.augment import AugmentPipe
from model.differentiable_renderer import DifferentiableRenderer
from model.graph import pool, unpool
from util.misc import boxblur_mask_k_k


@hydra.main(config_path='../config', config_name='stylegan2')
def test_dataloader(config):
    dataset = FaceGraphMeshDataset(config)
    dataloader = GraphDataLoader(dataset, batch_size=1, num_workers=0)
    render_helper = DifferentiableRenderer(config.image_size).cuda()
    for batch_idx, batch in enumerate(tqdm(dataloader)):
        # sanity test render + target colors
        batch = to_device(batch, torch.device("cuda:0"))
        print(batch['name'])
        rendered_color_gt = render_helper.render(batch['vertices'], batch['indices'], to_vertex_colors_scatter(batch["y"], batch), batch["ranges"].cpu())
        save_image(rendered_color_gt.permute((0, 3, 1, 2)), "test_dataloader_fake.png", nrow=4, value_range=(-1, 1), normalize=True)
        # sanity test graph counts and pool maps
        x_0 = pool(batch['y'], batch['graph_data']['node_counts'][0], batch['graph_data']['pool_maps'][0])
        x_1 = pool(x_0, batch['graph_data']['node_counts'][1], batch['graph_data']['pool_maps'][1])
        x_1 = unpool(x_1, batch['graph_data']['pool_maps'][1])
        x_0 = unpool(x_1, batch['graph_data']['pool_maps'][0])
        # works only if uv's are present
        # save_image(dataset.to_image(batch["y"], batch["graph_data"]["level_masks"][0]), "test_target.png", nrow=4, value_range=(-1, 1), normalize=True)
        # save_image(dataset.to_image(x_0, batch["graph_data"]["level_masks"][0]), "test_pooled.png", nrow=4, value_range=(-1, 1), normalize=True)
        # break


@hydra.main(config_path='../config', config_name='stylegan2')
def test_view_angles_together(config):
    import torchvision
    from dataset.mesh_real_features_patch import FaceGraphMeshDataset
    dataset = FaceGraphMeshDataset(config)
    dataloader = GraphDataLoader(dataset, batch_size=8, num_workers=0)
    render_helper = DifferentiableRenderer(config.image_size, 'bounds', config.colorspace, num_channels=4).cuda()
    augment_pipe = AugmentPipe(config.ada_start_p, config.ada_target, config.ada_interval, config.ada_fixed, config.batch_size, config.views_per_sample, config.colorspace).cuda()
    Path("runs/images_compare").mkdir(exist_ok=True)
    for batch_idx, batch in enumerate(tqdm(dataloader)):
        batch = to_device(batch, torch.device("cuda:0"))
        real = augment_pipe(torch.cat([dataset.get_color_bg_real(batch), batch['mask']], 1))
        rendered_color_gt = render_helper.render(batch['vertices'], batch['indices'], to_vertex_colors_scatter(batch["y"], batch), batch["ranges"].cpu(), batch['bg']).permute((0, 3, 1, 2))
        rendered_color_gt_image = rendered_color_gt[:, :3, :, :]
        real_image = dataset.cspace_convert_back(real[:, :3, :, :])
        real_mask = real[:, 3:4, :, :]
        # real_mask = boxblur_mask_k_k(real_mask, 21)
        rendered_color_gt_image = dataset.cspace_convert_back(rendered_color_gt_image)
        save_image(torch.cat([real_image, real_mask.expand(-1, 3, -1, -1), rendered_color_gt_image]), f"runs/images_compare/test_view_{batch_idx:04d}.png", nrow=4, value_range=(-1, 1), normalize=True)


@hydra.main(config_path='../config', config_name='stylegan2')
def test_view_angles_fake(config):
    dataset = FaceGraphMeshDataset(config)
    dataloader = GraphDataLoader(dataset, batch_size=8, num_workers=0)
    render_helper = DifferentiableRenderer(config.image_size, 'bounds').cuda()
    Path("runs/images_fake").mkdir(exist_ok=True)
    for batch_idx, batch in enumerate(tqdm(dataloader)):
        batch = to_device(batch, torch.device("cuda:0"))
        rendered_color_gt = render_helper.render(batch['vertices'], batch['indices'], to_vertex_colors_scatter(batch["y"], batch), batch["ranges"].cpu())
        save_image(rendered_color_gt.permute((0, 3, 1, 2)), f"runs/images_fake/test_view_{batch_idx:04d}.png", nrow=4, value_range=(-1, 1), normalize=True)


@hydra.main(config_path='../config', config_name='stylegan2')
def test_view_angles_real(config):
    dataset = FaceGraphMeshDataset(config)
    dataloader = GraphDataLoader(dataset, batch_size=32, num_workers=0)
    Path("runs/images_real").mkdir(exist_ok=True)
    for batch_idx, batch in enumerate(tqdm(dataloader)):
        save_image(batch['real'], f"runs/images_real/test_view_{batch_idx:04d}.png", nrow=8, value_range=(-1, 1), normalize=True)


@hydra.main(config_path='../config', config_name='stylegan2')
def test_masks(config):
    import numpy as np
    from PIL import Image
    for mask in tqdm(list(Path(config.mask_path).iterdir())):
        if np.array(Image.open(mask)).sum() == 0:
            print(mask)


if __name__ == '__main__':
    test_view_angles_together()
