import collections
import io
import os
import shutil
from typing import Any, Dict, Tuple

import boto3
import json
import torch
import torchvision
from torchvision.transforms import Compose, ToTensor

from PIL import Image
import xml.etree.ElementTree as ET

from torchvision import transforms
from torch.utils.data import Dataset, DataLoader


import pyarrow.parquet as pq


def get_test_transforms():
    return transforms.Compose([
        transforms.Resize(240),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
    ])


def readimage(path):
        count = os.stat(path).st_size / 2
        with open(path, "rb") as f:
            return bytearray(f.read())


def get_transform():
    transforms = []
    transforms.append(ToTensor())
    return Compose(transforms)


def collate_fn(batch):
    return tuple(zip(*batch))


class VOCParquetDataset(Dataset):
    def __init__(self,
                 root,
                 transforms=get_transform()):
        self.transforms = transforms
        dataset = pq.ParquetDataset(root)
        self.table = dataset.read()

        self.class_names = [
            "aeroplane",
            "bicycle",
            "bird",
            "boat",
            "bottle",
            "bus",
            "car",
            "cat",
            "chair",
            "cow",
            "diningtable",
            "dog",
            "horse",
            "motorbike",
            "person",
            "pottedplant",
            "sheep",
            "sofa",
            "train",
            "tvmonitor",
        ]
        self.NUM_CLASSES = len(self.class_names)
        self.name2number = {}
        self.number2name = {}
        for i, name in enumerate(self.class_names):
            self.name2number[name] = i
            self.number2name[i] = name

    def __getitem__(self, idx):
        """
        Args:
            index (int): Index

        Returns:
            tuple: (image, target) where target is a dictionary of the XML tree.
        """
        example = self.table.slice(idx,1).to_pydict()
        img = Image.open(io.BytesIO(example['image'][0])).convert('RGB')
        annotation = example['annotations'][0]

        anno_dict = self.parse_voc_xml(
            ET.fromstring(annotation))

        labels = []
        boxes = []
        for obj in anno_dict['annotation']['object']:
            labels.append(self.name2number[obj['name']])
            bb = obj['bndbox']
            bbox = [float(bb["xmin"]), float(bb["ymin"]), float(bb["xmax"]), float(bb["ymax"])]
            boxes.append(bbox)

        device = torch.device('cuda')

        boxes = torch.as_tensor(boxes, dtype=torch.float32).to(device)
        labels = torch.as_tensor(labels, dtype=torch.int64).to(device)
        if self.transforms is not None:
            img = self.transforms(img)

        return img.to(device), {'labels': labels, 'boxes': boxes}

    def __len__(self):
        return self.table.num_rows

    def parse_voc_xml(self, node):
        voc_dict = {}
        children = list(node)
        if children:
            def_dic = collections.defaultdict(list)
            for dc in map(self.parse_voc_xml, children):
                for ind, v in dc.items():
                    def_dic[ind].append(v)
            if node.tag == 'annotation':
                def_dic['object'] = [def_dic['object']]
            voc_dict = {
                node.tag:
                    {ind: v[0] if len(v) == 1 else v
                     for ind, v in def_dic.items()}
            }
        if node.text:
            text = node.text.strip()
            if not children:
                voc_dict[node.tag] = text
        return voc_dict


def download_s3_dir(bucket_name, directory, download_dir):
    s3_resource = boto3.resource('s3')
    bucket = s3_resource.Bucket(bucket_name)
    for object in bucket.objects.filter(Prefix=directory):
        path = os.path.join(download_dir, object.key)
        if not os.path.exists(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        bucket.download_file(object.key, path)


def download_parquet(bucket, table, files, save_path):
    s3 = boto3.client('s3')
    table_path = os.path.join(save_path, table)
    if not os.path.exists(table_path):
        os.makedirs(table_path, exist_ok=True)
    for file in files:
        path = os.path.join(table, file)
        s3.download_file(bucket, path, os.path.join(save_path, path))
    return table_path

def download_version(table_path, bucket, version=0, save_path='./'):
    if os.path.exists(table_path):
        shutil.rmtree(table_path)
    delta_config_folder = "_delta_log"
    config_path = os.path.join(table_path, delta_config_folder)
    download_s3_dir(bucket, config_path, save_path)
    local_config_dir = os.path.join(save_path, table_path, delta_config_folder)
    files = set()
    for v in range(version+1):
        filename = str(v).zfill(20) + '.json'
        with open(os.path.join(local_config_dir, filename)) as f:
            for line in f:
                info = json.loads(line.strip())
                if 'add' in info:
                    files.add(info['add']['path'])
                if 'remove' in info:
                    files.remove(info['remove']['path'])
    return download_parquet(bucket, table_path, files, save_path)
