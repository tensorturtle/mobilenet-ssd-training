import numpy as np
import pathlib
import cv2
import pandas as pd
import copy
import os
import logging

from ..utils.misc import xywh_to_xyxy


# visualization
from vision.utils.visualization import plot_image_grid, make_square

# COCO Dataset
from pycocotools.coco import COCO # install from cocoapi

class COCODataset:

    def __init__(self, root,
                 transform=None, target_transform=None,
                 dataset_type="train", balance_data=False, viz_inputs=False):
        self.root = pathlib.Path(root)
        self.dataset_type = dataset_type.lower()
        self.coco = self._load_cocoapi()
        self.transform = transform
        self.target_transform = target_transform

        self.data, self.class_names, self.class_dict = self._read_data()
        self.balance_data = balance_data
        self.min_image_num = -1
        if self.balance_data:
            self.data = self._balance_data()
        self.ids = [info['image_id'] for info in self.data]

        self.class_stat = None

        self.id_to_filename = self._id_to_filename()
        self.viz_inputs = viz_inputs

    def _load_cocoapi(self):
        annotation_file = os.path.join(self.root, 'annotations', f'instances_{self.dataset_type}2017.json')
        logging.info(f'Loading annotations from {annotation_file}')

        # initialize COCO API for instance annotations
        return COCO(annotation_file)

    def _id_to_filename(self):
        convert_dict = {}

        for img_id in self.coco.getImgIds():
            img_filename = self.coco.loadImgs([img_id])[0]['file_name']
            convert_dict.update({img_id : img_filename})

        return convert_dict

    def _getitem(self, index):
        image_info = self.data[index]
        image = self._read_image(image_info['image_id'])
        # duplicate boxes to prevent corruption of dataset
        boxes = copy.copy(image_info['boxes'])
        # duplicate labels to prevent corruption of dataset
        labels = copy.copy(image_info['labels'])

        # Compare images before and after transform
        if self.viz_inputs: #BEFORE
            image_before = copy.copy(image)
            sq_image_before = make_square(image_before, (0,0,0), 300) # change for SSD-512 or others that aren't 300 square

        if self.transform:
            image, boxes, labels = self.transform(image, boxes, labels)

        if self.viz_inputs:
            image_after = np.moveaxis(image.numpy(), 0, -1)
            image_overlay = copy.copy(image_after)
            for i in range(boxes.shape[0]):
                box = boxes[i]
                cv2.rectangle(image_overlay,
                        (int(box[0]), int(box[1])),
                        (int(box[2]), int(box[3])),
                        (255,255,0),
                        4)
                label = f"{labels[i]}"
                cv2.putText(image_overlay,
                        label,
                        (int(box[0]) + 20, int(box[1]) + 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, #font scale
                        (255,0,255),
                        2) # line type

            plot_image_grid([sq_image_before, image_after, image_overlay])


        # boxes normalized from pixels to [0,1] range
        width = image.shape[1] # ACTUALLY, not sure which one is which
        height = image.shape[2]
        boxes[:,0] /= width
        boxes[:,1] /= height
        boxes[:,2] /= width
        boxes[:,3] /= height


        if self.target_transform:
            boxes, labels = self.target_transform(boxes, labels)


        return image_info['image_id'], image, boxes, labels

    def __getitem__(self, index):
        _, image, boxes, labels = self._getitem(index)
        return image, boxes, labels

    def get_annotation(self, index):
        """To conform the eval_ssd implementation that is based on the VOC dataset."""
        image_id, image, boxes, labels = self._getitem(index)
        is_difficult = np.zeros(boxes.shape[0], dtype=np.uint8)
        return image_id, (boxes, labels, is_difficult)

    def get_image(self, index):
        image_info = self.data[index]
        image = self._read_image(image_info['image_id'])
        if self.transform:
            image, _ = self.transform(image)
        return image

    def _read_data(self):

        # get COCO categories
        cats = self.coco.loadCats(self.coco.getCatIds())
        pure_class_names = [cat['name'] for cat in cats]
        class_names = ['BACKGROUND'] + pure_class_names # in index 0 of this list, 'BACKGROUND' is an empty category. It is used internally by SSD as the label for prior bboxes that aren't associated with any object
        class_dict = {class_name : i for i,class_name in enumerate(class_names)}

        # filling out data list
        all_image_ids = self.coco.getImgIds()

        # how many images were skipped (nonexistent file)
        skipped_images = 0

        # the following for loop fills this data list
        data = []
            # python list containing:
            #   list of dictionaries for each image
            #       where keys: 'image_id', 'boxes', 'labels'
            #       value for 'image_id' is String
            #       value for 'boxes' is a 2D numpy array, dtype=float32
            #       value for 'labels' is a 1D numpy array, dtype= python int64

        for image_id in all_image_ids:

            # check existence  of image file; otherwise skip that image_id
            img_info = self.coco.loadImgs(ids=[image_id])
            img_filename = img_info[0]['file_name']
            img_path = os.path.join(self.root, self.dataset_type + '2017', img_filename)
            if os.path.isfile(img_path) is False:
                logging.error(f'Skipping image_id: {image_id}')
                skipped_images += 1
                continue

            boxes = []
            labels = []

            ann_ids = self.coco.getAnnIds(imgIds=[image_id])
            ann = self.coco.loadAnns(ann_ids)
            for instance in ann:
                boxes.append(instance['bbox'])
                labels += [instance['category_id']]

            # convert coco annotation data format to be compatible with open_images
            boxes = self._xywh_to_xyxy(boxes)

            # convert to numpy arrays
            boxes = np.array(boxes, dtype=np.float32)
            labels = np.array(labels, dtype='int64')

            # convert bounding boxes from (x,y,width,height)pixels to (Xmin, Ymin, Xmax, Ymax)pixels format

            data.append({
                'image_id': image_id,
                'boxes' : boxes,
                'labels' : labels
                })

        logging.warning(f'Out of {len(all_image_ids)} images, {skipped_images} have been skipped, leaving {len(all_image_ids) - skipped_images} to be used. \n\tSkipped images either have no annotation (and moved to an adjacent folder) or simply missing.')
        return data, class_names, class_dict

    def _xywh_to_xyxy(self, boxes: list) -> list:
        '''
        xywh: x_of_left_top_corner, y_of_left_top_corner, width, height in pixels
        xyxy: Xmin, Ymin, Xmax, Ymax in pixels

        this function takes in a list of lists
        and returns a list of lists
        '''
        xyxy_boxes = []
        for bbox in boxes:
            xyxy_boxes.append(xywh_to_xyxy(bbox))
        return xyxy_boxes

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        if self.class_stat is None:
            self.class_stat = {name: 0 for name in self.class_names[:]}
            for example in self.data:
                for class_index in example['labels']:
                    class_name = self.class_names[class_index]
                    self.class_stat[class_name] += 1
        content = ["Dataset Summary:"
                   f"Number of Images: {len(self.data)}",
                   f"Minimum Number of Images for a Class: {self.min_image_num}",
                   "Label Distribution:"]
        for class_name, num in self.class_stat.items():
            content.append(f"\t{class_name}: {num}")
        return "\n".join(content)

    def _read_image(self, image_id):
        image_file = os.path.join(self.root, self.dataset_type + '2017', self.id_to_filename[image_id])
        # loading with opencv
        image = cv2.imread(str(image_file))
        if image.shape[2] == 1:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        else:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


        return image
