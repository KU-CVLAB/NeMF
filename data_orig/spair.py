import json
import glob
import os
import cv2
import torch
from .semantic_keypoints_datasets import SemanticKeypointsDataset, random_crop
from data_orig.semantic_keypoints_datasets import pad_to_same_shape
from data_orig.semantic_keypoints_datasets import define_mask_zero_borders
import random


class SPairDataset(SemanticKeypointsDataset):
    """Spair dataset"""
    def __init__(self,  root, split, thres='bbox', source_image_transform=None,
                 target_image_transform=None, flow_transform=None, training_cfg=None, output_image_size=None):
        """
        Args:
            root:
            split:
            thres:
            source_image_transform: image transformations to apply to source images
            target_image_transform: image transformations to apply to target images
            flow_transform: flow transformations to apply to ground-truth flow fields
            training_cfg: training config
            output_image_size: size if images and annotations need to be resized, used when split=='test'
        Output in __getittem__ (for split=='test'):
            src_img
            trg_img
            source_image_size
            target_image_size
            flow_map
            correspondence_mask: valid correspondences (which are originally sparse)
            src_kps
            trg_kps
        """
        super(SPairDataset, self).__init__('spair', root, thres, split, source_image_transform,
                                           target_image_transform, flow_transform, training_cfg=training_cfg)

        self.train_data = open(self.spt_path).read().split('\n')
        self.train_data = self.train_data[:len(self.train_data) - 1]
        self.src_imnames = list(map(lambda x: x.split('-')[1] + '.jpg', self.train_data))
        self.trg_imnames = list(map(lambda x: x.split('-')[2].split(':')[0] + '.jpg', self.train_data))
        self.cls = os.listdir(self.img_path)
        self.cls.sort()

        anntn_files = []
        for data_name in self.train_data:
            anntn_files.append(glob.glob('%s/%s.json' % (self.ann_path, data_name))[0])
        anntn_files = list(map(lambda x: json.load(open(x)), anntn_files))
        self.src_kps = list(map(lambda x: torch.tensor(x['src_kps']).t().float(), anntn_files))
        self.trg_kps = list(map(lambda x: torch.tensor(x['trg_kps']).t().float(), anntn_files))
        self.src_bbox = list(map(lambda x: torch.tensor(x['src_bndbox']).float(), anntn_files))
        self.trg_bbox = list(map(lambda x: torch.tensor(x['trg_bndbox']).float(), anntn_files))
        self.cls_ids = list(map(lambda x: self.cls.index(x['category']), anntn_files))

        self.vpvar = list(map(lambda x: torch.tensor(x['viewpoint_variation']), anntn_files))
        self.scvar = list(map(lambda x: torch.tensor(x['scale_variation']), anntn_files))
        self.trncn = list(map(lambda x: torch.tensor(x['truncation']), anntn_files))
        self.occln = list(map(lambda x: torch.tensor(x['occlusion']), anntn_files))

        # if need to resize the images, even for testing
        if output_image_size is not None:
            if not isinstance(output_image_size, tuple):
                output_image_size = (output_image_size, output_image_size)
        self.output_image_size = output_image_size

    def __getitem__(self, idx):
        """
        Args:
            idx:

        Returns: for split is 'test', dictionary with fieldnames:
            src_img
            trg_img
            source_image_size
            target_image_size
            flow_map
            correspondence_mask: valid correspondences (which are originally sparse)
            src_kps
            trg_kps
        """
        batch = super(SPairDataset, self).__getitem__(idx)

        batch['src_bbox'] = self.get_bbox(self.src_bbox, idx, original_image_size=batch['src_imsize_ori'])
        batch['trg_bbox'] = self.get_bbox(self.trg_bbox, idx, original_image_size=batch['src_imsize_ori'])

        if self.split != 'test':
            # for training, might want to have different output flow sizes
            if self.training_cfg['augment_with_crop']:
                batch['src_img'], batch['src_kps'], batch['src_bbox'] = random_crop(
                    batch['src_img'], batch['src_kps'].clone(), batch['src_bbox'].int(),
                    size=self.training_cfg['crop_size'], p=self.training_cfg['proba_of_crop'])

                batch['trg_img'], batch['trg_kps'], batch['trg_bbox'] = random_crop(
                    batch['trg_img'], batch['trg_kps'].clone(), batch['trg_bbox'].int(),
                    size=self.training_cfg['crop_size'], p=self.training_cfg['proba_of_crop'])

            if self.training_cfg['augment_with_flip']:
                if random.random() < self.training_cfg['proba_of_batch_flip']:
                    self.horizontal_flip(batch)
                else:
                    if random.random() < self.training_cfg['proba_of_image_flip']:
                        batch['src_img'], batch['src_bbox'], batch['src_kps'] = self.horizontal_flip_img(
                            batch['src_img'], batch['src_bbox'], batch['src_kps'])
                    if random.random() < self.training_cfg['proba_of_image_flip']:
                        batch['trg_img'], batch['trg_bbox'], batch['trg_kps'] = self.horizontal_flip_img(
                            batch['trg_img'], batch['trg_bbox'], batch['trg_kps'])

            batch = self.recover_image_pair_for_training(batch)
            batch['src_bbox'] = self.get_bbox(self.src_bbox, idx, batch['src_imsize_ori'],
                                              output_image_size=self.training_cfg['output_image_size'])
            batch['trg_bbox'] = self.get_bbox(self.trg_bbox, idx, batch['trg_imsize_ori'],
                                              output_image_size=self.training_cfg['output_image_size'])
            batch['pckthres'] = self.get_pckthres(batch, batch['source_image_size'])

            if self.source_image_transform is not None:
                batch['src_img'] = self.source_image_transform(batch['src_img'])
            if self.target_image_transform is not None:
                batch['trg_img'] = self.target_image_transform(batch['trg_img'])

            flow = batch['flow_map']
            if self.flow_transform is not None:
                if type(flow) in [tuple, list]:
                    # flow field at different resolution
                    for i in range(len(flow)):
                        flow[i] = self.flow_transform(flow[i])
                else:
                    flow = self.flow_transform(flow)
            batch['flow_map'] = flow

            if self.training_cfg['compute_mask_zero_borders']:
                mask_valid = define_mask_zero_borders(batch['trg_img'])
                batch['mask_zero_borders'] = mask_valid
        else:
            batch['src_bbox'] = self.get_bbox(self.src_bbox, idx, batch['src_imsize_ori'],
                                              output_image_size=self.output_image_size)
            batch['trg_bbox'] = self.get_bbox(self.trg_bbox, idx, batch['trg_imsize_ori'],
                                              output_image_size=self.output_image_size)
            batch['pckthres'] = self.get_pckthres(batch, batch['source_image_size'])

            # batch['src_img'], batch['trg_img'] = pad_to_same_shape(batch['src_img'],
            #                                                                  batch['trg_img'])

            batch['vpvar'] = self.vpvar[idx]
            batch['scvar'] = self.scvar[idx]
            batch['trncn'] = self.trncn[idx]
            batch['occln'] = self.occln[idx]

            h_size, w_size = batch['trg_img'].shape[:2]
            flow, mask = self.keypoints_to_flow(batch['src_kps'][:batch['n_pts']],
                                                batch['trg_kps'][:batch['n_pts']], h_size=h_size, w_size=w_size)
            if self.source_image_transform is not None:
                batch['src_img'] = self.source_image_transform(batch['src_img'])
            if self.target_image_transform is not None:
                batch['trg_img'] = self.target_image_transform(batch['trg_img'])
            if self.flow_transform is not None:
                flow = self.flow_transform(flow)

            batch['flow_map'] = flow
            batch['correspondence_mask'] = mask.bool() if float(torch.__version__[:3]) >= 1.1 else mask.byte()

        return batch

    def get_image(self, img_names, idx):
        r"""Returns image tensor"""
        path = os.path.join(self.img_path, self.cls[self.cls_ids[idx]], img_names[idx])

        return cv2.imread(path)[:, :, ::-1]

    def get_bbox(self, bbox_list, idx, original_image_size=None, output_image_size=None):
        r"""Returns object bounding-box"""
        bbox = bbox_list[idx].clone()
        if self.output_image_size is not None:
            if output_image_size is None:
                bbox[0::2] *= (self.output_image_size[1] / original_image_size[1])
                bbox[1::2] *= (self.output_image_size[0] / original_image_size[0])
            else:
                bbox[0::2] *= (output_image_size[1] / original_image_size[1])
                bbox[1::2] *= (output_image_size[0] / original_image_size[0])
        return bbox