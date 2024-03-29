# ISS VSE CA1
# Kenneth Goh Chia Wei : A0198544N  
# Tan Heng Han : A0198502B  
# Raymond Ng Boon Cheong : A0198543R
#%%
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import cv2
import tensorflow as tf
import os
import xml.etree.ElementTree as ET
import pickle
import random as rand
import copy
import glob

# import imgaug as ia
from imgaug import augmenters as iaa
from scipy.special import expit #sigmoid

from keras import regularizers
from keras.models import Model
from keras.layers import Conv2D
from keras.layers import Input
from keras.layers import BatchNormalization
from keras.layers import LeakyReLU
from keras.layers import ZeroPadding2D
from keras.layers import UpSampling2D
from keras.layers import AveragePooling2D
from keras.layers import Flatten
from keras.layers.merge import add
from keras.layers.merge import concatenate
from keras.applications.nasnet import preprocess_input
from keras.engine.topology import Layer
from keras.utils import Sequence
from keras.callbacks import EarlyStopping
from keras.callbacks import ReduceLROnPlateau
from keras.callbacks import BaseLogger
from keras.callbacks import ModelCheckpoint
from keras.optimizers import Adam

#%%
#---------- API for YoloV3 ----------
class YoloV3_API():
    def __init__(self, img_dir, annotation_dir, saved_model_name, train_size, height=416, width=416, threshold=0.5, batch_size=16, shuffle=True, pretrained_weights='Yolov3_pretrained_weights.h5'):
        '''Ctor'''
        self.saved_model_name=saved_model_name
        self.threshold=threshold
        self.height=height
        self.width=width
        print(f'Image directory: {img_dir}')
        print(f'Annotation directory: {annotation_dir}')
        print(f'Saved model name: {self.saved_model_name}')
        print(f'Train/validation size ratio: {str(train_size)}')
        print(f'Training size: {str(height)}x{str(width)}')
        print(f'Threshold: {str(threshold)}')
        print(f'Batch Size: {str(batch_size)}\n')

        self.all_anno, self.labels, self.anchor_boxes = self._process_dataset(img_dir, annotation_dir)
        print(f'All Image and annotation size: {len(self.all_anno)}')
        print(f'Unique labels: {str(self.labels)}')
        print(f'Generated Anchor Boxes: {str(self.anchor_boxes)}\n')
        
        self.train_anno, self.valid_anno, self.max_boxes = self._train_valid_split(self.all_anno, train_size)
        print(f'Training image and annotation size: {len(self.train_anno)}')
        print(f'Validation image and annotation size: {len(self.valid_anno)}')
        print(f'Maximum bounding boxes in all images: {str(self.max_boxes)}\n')

        self.train_generator = DataGenerator(
            annotations=self.train_anno,
            max_boxes=self.max_boxes,
            anchors=self.anchor_boxes,
            labels=self.labels,
            batch_size=batch_size,
            width=width,
            height=height,
            shuffle=shuffle)
        print('Train Generator created: To access, use <YoloV3_API.train_generator>')

        self.valid_generator = DataGenerator(
            annotations=self.valid_anno,
            max_boxes=self.max_boxes,
            anchors=self.anchor_boxes,
            labels=self.labels,
            batch_size=batch_size,
            width=width,
            height=height,
            shuffle=shuffle)
        print('Validation Generator created: To access, use <YoloV3_API.valid_generator>\n')

        self.train_model, self.infer_model = YoloV3(
            numcls=len(self.labels),
            anchors=self.anchor_boxes,
            max_grid=[416, 416],
            batch_size=batch_size,
            threshold=threshold,
            max_boxes=self.max_boxes)

        print('Loading pretrained weights')
        self.train_model.load_weights(pretrained_weights, by_name=True)

        print(f'YOLOv3 Training Model created: To access, use <YoloV3_API.train_model>')
        print(f'\nYOLOv3 Inference Model created: To access, use <YoloV3_API.infer_model>\n')
        print('Train Model Summary')
        print(self.train_model.summary())
        print('\Inference Model Summary')
        print(self.infer_model.summary())
        
    def fit_generator(self, epoch=300, lr=1e-4):
        '''Fit with augmentation'''
        callbacks = create_callbacks(self.saved_model_name)

        def dummy_loss(y_true, y_pred):
            return tf.sqrt(tf.reduce_sum(y_pred))

        opt = Adam(lr=lr)
        self.train_model.compile(loss=dummy_loss, optimizer=opt)

        history = self.train_model.fit_generator(
            generator=self.train_generator,
            validation_data=self.valid_generator,
            steps_per_epoch=len(self.train_generator),
            epochs=epoch,
            callbacks=callbacks)
        return history

    def predict(self, img_path):
        '''Predict image using inference model'''
        # Load weights to inference model
        self._load_weights_to_infer_model()

        # Read image
        image = cv2.imread(img_path)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        img = self._preprocess_input(image)
               
        # Predict image
        pred = self.infer_model.predict(img)
        pred_boxes = self.process_prediction(image, pred)
        return image, pred_boxes
    
    def process_prediction(self, img, pred):
        # Process predicted bounding boxes
        img_height, img_width, _ = img.shape
        for lyr in range(len(pred)):
            currPred = pred[lyr][0]
            lyr_anchors = self.anchor_boxes[(2 - lyr) * 6 : (3 - lyr) * 6]
            grid_height, grid_width = currPred.shape[:2]
            box_count = 3 # 3 bounding boxes per cell
            currPred = currPred.reshape((grid_height, grid_width, box_count, -1))            

            pred_boxes = []
            currPred[...,:2] = expit(currPred[..., :2])
            currPred[..., 4] = expit(currPred[..., 4])
            currPred[..., 5:] = currPred[..., 4][..., np.newaxis] * self._softmax(currPred[..., 5:])
            currPred[..., 5:] *= currPred[..., 5:] > self.threshold

            for cell in range(grid_height * grid_width): # loop thru each cell
                row, col = cell // grid_width, cell % grid_width
                for box in range(box_count):
                    objectiveness_score = currPred[row, col, box, 4]
                    if (objectiveness_score <= self.threshold): continue # Skip if objectiveness score too low
                    x, y, w, h = currPred[row, col, box, :4]
                    x = (col + x) / grid_width
                    y = (row + y) / grid_height
                    w = lyr_anchors[2*box] * np.exp(w) / self.width
                    h = lyr_anchors[2*box+1] * np.exp(h) / self.height

                    classes = currPred[row, col, box, 5:]
                    predbox = BoundingBox(
                        xmin=x-w/2,
                        xmax=x+w/2,
                        ymin=y-h/2,
                        ymax=y+h/2,
                        objectiveness_score=objectiveness_score,
                        classes=classes)
                    pred_boxes.append(predbox)
        
        # Fix bounding box scale
        self._scale_predicted_boxes(pred_boxes, img_height, img_width)
        self._non_max_suppression(pred_boxes, 0.4)
        return pred_boxes

    def draw_prediction(self, img, predicted_boxes):
        labels = list(self.labels)
        for box in predicted_boxes:
            label_str = ''
            label_idx = -1
            for idx in range(len(labels)):
                if box.classes[idx] > self.threshold:
                    if label_str != '':
                        label_str += ', '
                    label_str += (labels[idx] + ' ' + str(round(box.get_score()*100, 2)) + '%')
                    label_idx = idx
            if label_idx >= 0:
                cv2.rectangle(img=img, pt1=(box.xmin,box.ymin), pt2=(box.xmax,box.ymax), color=(255,0,0), thickness=4)
                text_size = cv2.getTextSize(label_str, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 5)
                cv2.putText(img=img, 
                        text=label_str, 
                        org=(box.xmin+13, box.ymin-13), 
                        fontFace=cv2.FONT_HERSHEY_SIMPLEX, 
                        fontScale=0.5, 
                        color=(255,0,0), 
                        thickness=2)
        return img

    def _preprocess_input(self, image):
        img_height, img_width, _ = image.shape

        # determine the new size of the image
        if (float(self.width)/img_width) < (float(self.height)/img_height):
            img_height = (img_height * self.width)//img_width
            img_width = self.width
        else:
            img_width = (img_width * self.height)//img_height
            img_height = self.height

        # resize the image to the new size
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(image/255., (img_width, img_height))

        # embed the image into the standard letter box
        new_image = np.ones((self.height, self.width, 3)) * 0.5
        new_image[(self.height-img_height)//2:(self.height+img_height)//2, (self.width-img_width)//2:(self.width+img_width)//2, :] = resized
        new_image = np.expand_dims(new_image, 0)
        return new_image

    # https://stackoverflow.com/questions/34968722/how-to-implement-the-softmax-function-in-python
    def _softmax(self, x, axis=-1):
        '''Calculate softmax'''
        x = x - np.amax(x, axis, keepdims=True)
        e_x = np.exp(x)
        return e_x / e_x.sum(axis, keepdims=True)
    
    def _scale_predicted_boxes(self, boxes, image_height, image_width):
        if (float(self.width)/image_width) < (float(self.height)/image_height):
            height = (image_height * self.width) / image_width
            width = self.width
        else:
            height = self.width
            width = (image_width * self.height) / image_height
            
        for i in range(len(boxes)):
            xOffset = (self.width - width)/2./self.width
            xScale = float(width)/self.width
            yOffset = (self.height - height)/2./self.height 
            yScale = float(height)/self.height
            
            boxes[i].xmin = int((boxes[i].xmin - xOffset) / xScale * image_width)
            boxes[i].xmax = int((boxes[i].xmax - xOffset) / xScale * image_width)
            boxes[i].ymin = int((boxes[i].ymin - yOffset) / yScale * image_height)
            boxes[i].ymax = int((boxes[i].ymax - yOffset) / yScale * image_height)

    def _non_max_suppression(self, boxes, threshold):
        '''Perform non max suppression on bounding boxes'''
        if len(boxes) > 0:
            nb_class = len(boxes[0].classes)
        else:
            return
        
        for c in range(nb_class):
            sorted_indices = np.argsort([-box.classes[c] for box in boxes])

            for i in range(len(sorted_indices)):
                index_i = sorted_indices[i]

                if boxes[index_i].classes[c] == 0: continue

                for j in range(i+1, len(sorted_indices)):
                    index_j = sorted_indices[j]

                    if calculate_bb_iou(boxes[index_i], boxes[index_j]) >= threshold:
                        boxes[index_j].classes[c] = 0

    def _load_weights_to_infer_model(self):
        '''Load saved weights to inference model'''
        try:
            self.infer_model.load_weights(self.saved_model_name)
        except:
            print('Error loading weights to inference model!')
            return

    def _process_dataset(self, img_dir, annotation_dir):
        '''Read all annotation files and generate anchor boxes'''
        all_anno, labels = read_annotation_files(img_dir, annotation_dir)
        anchor_boxes = generateAnchorBoxes(all_anno, labels)
        return all_anno, labels, anchor_boxes

    def _train_valid_split(self, all_anno, train_size=0.8):
        '''Split dataset to train test split'''
        train_valid_split = int(train_size * len(all_anno))
        np.random.shuffle(all_anno)
        train_anno = all_anno[:train_valid_split]
        valid_anno = all_anno[train_valid_split:]
        max_boxes = max([len(anno['object']) for anno in (train_anno + valid_anno)])
        return train_anno, valid_anno, max_boxes
#------------------------------------
#----------Bounding Box class and utils----------
#%%
class BoundingBox:
    def __init__(self, xmin, xmax, ymin, ymax, objectiveness_score=None, classes=None, label=None):
        self.xmin=xmin
        self.xmax=xmax
        self.ymin=ymin
        self.ymax=ymax
        self.objectiveness_score=objectiveness_score
        self.classes=classes
        self.label=label
        self.score=-1
    
    def __str__(self):
        return f'xmin: {self.xmin}, xmax: {self.xmax}, ymin: {self.ymin}, ymax: {self.ymax}, objectiveness: {self.objectiveness_score}, classes: {self.classes}, label: {self.label}'
    
    def get_label(self):
        if self.label == None:
            self.label = np.argmax(self.classes)
        
        return self.label
    
    def get_score(self):
        if self.score == -1:
            self.score = self.classes[self.get_label()]
            
        return self.score    

def calculate_bb_iou(boundbox1, boundbox2):
    if boundbox2.xmin < boundbox1.xmin:
        if boundbox2.xmax < boundbox1.xmin:
            xIntersect = 0
        else:
            xIntersect = min(boundbox1.xmax, boundbox2.xmax) - boundbox1.xmin
    else:
        if boundbox1.xmax < boundbox2.xmin:
            xIntersect = 0
        else:
            xIntersect = min(boundbox1.xmax, boundbox2.xmax) - boundbox2.xmin
    if boundbox2.ymin < boundbox1.ymin:
        if boundbox2.ymax < boundbox1.ymin:
            yIntersect = 0
        else:
            yIntersect = min(boundbox1.ymax, boundbox2.ymax) - boundbox1.ymin
    else:
        if boundbox1.ymax < boundbox2.ymin:
            yIntersect = 0
        else:
            yIntersect = min(boundbox1.ymax, boundbox2.ymax) - boundbox2.ymin
    
    intersect = xIntersect * yIntersect
    union = ((boundbox1.xmax - boundbox1.xmin) * (boundbox1.ymax - boundbox1.ymin)) + ((boundbox2.xmax - boundbox2.xmin) * (boundbox2.ymax - boundbox2.ymin)) - intersect
    return float(intersect) / union
#------------------------------------------------
#%%
#----------VOC Parser----------
def read_annotation_files(image_dir, annnotation_dir):
    all_annotations = []
    labels = {}
    for anno in sorted(os.listdir(annnotation_dir)):
        img = {'object':[]}
        try:
            xml_tree = ET.parse(annnotation_dir + anno)
        except Exception as ex:
            print(ex)
            print(f'Error parsing annotation xml: {annnotation_dir + anno}')
            continue
        for elem in xml_tree.iter():
            if 'filename' in elem.tag:
                img['filename'] = image_dir + elem.text
            if 'width' in elem.tag:
                img['width'] = int(elem.text)
            if 'height' in elem.tag:
                img['height'] = int(elem.text)
            if 'object' in elem.tag:
                obj = {}
                for attribute in list(elem):
                    if 'name' in attribute.tag:
                        obj['name'] = attribute.text
                        if obj['name'] in labels:
                            labels[obj['name']] += 1
                        else:
                            labels[obj['name']] = 1
                        img['object'] += [obj]
                    if 'bndbox' in attribute.tag:
                        for dim in list(attribute):
                            if 'xmin' in dim.tag:
                                obj['xmin'] = int(round(float(dim.text)))
                            if 'ymin' in dim.tag:
                                obj['ymin'] = int(round(float(dim.text)))
                            if 'xmax' in dim.tag:
                                obj['xmax'] = int(round(float(dim.text)))
                            if 'ymax' in dim.tag:
                                obj['ymax'] = int(round(float(dim.text)))
        if len(img['object']) > 0:
            bb_list = []
            for obj in img['object']:
                bb_list.append(BoundingBox(xmin=obj['xmin'], xmax=obj['xmax'], ymin=obj['ymin'], ymax=obj['ymax'], label=obj['name']))
            img['bbs'] = bb_list
            all_annotations += [img]
    return all_annotations, labels.keys()
#------------------------------
#%%
#----------Generate Anchor boxes----------
def calculate_iou(anno, centroids):
    '''Calculate IOU'''
    anno_width, anno_height = anno
    iou_list = []
    anno_area = anno_width * anno_height
    for cent in centroids:
        cent_width, cent_height = cent
        if cent_width <= anno_width and cent_height >= anno_height:
            iou = cent_width * anno_height / (anno_area + cent_width * (cent_height - anno_height))
        elif cent_width >= anno_width and cent_height >= anno_height:
            iou = anno_area / (cent_width * cent_height)
        elif cent_width >= anno_width and cent_height <= anno_height:
            iou = anno_width * cent_height / (anno_area + (cent_width - anno_width) * cent_height)
        else:
            iou = (cent_width * cent_height) / anno_area
        iou_list.append(iou)
    return np.array(iou_list)

def calculate_kMeans(anno_arr, num_anchor):
    '''Kmeans algorithm to determine anchor clusters'''
    curr_assign_centroids = np.zeros(anno_arr.shape[0])
    curr_distances = np.zeros((anno_arr.shape[0], num_anchor))
    indexes = [rand.randrange(anno_arr.shape[0]) for anchor in range(num_anchor)]
    sample_centroids = anno_arr[indexes]
    iterations = 0
    while True:
        iterations += 1
        distances = []
        for idx in range(anno_arr.shape[0]):
            distance = 1 - calculate_iou(anno_arr[idx], sample_centroids)
            distances.append(distance)
        distances_arr = np.array(distances)
        assign_centroids = np.argmin(distances_arr, axis=1)
        if (assign_centroids == curr_assign_centroids).all():
            return sample_centroids
        centroid_sums = np.zeros((num_anchor, anno_arr.shape[1]), np.float)
        for idx in range(anno_arr.shape[0]):
            centroid_sums[assign_centroids[idx]] += anno_arr[idx]
        for idx in range(num_anchor):
            sample_centroids[idx] = centroid_sums[idx] / (np.sum(assign_centroids==idx) + 1e-6)
        curr_assign_centroids = assign_centroids.copy()
        curr_distances = distances_arr.copy()

#Reference https://lars76.github.io/object-detection/k-means-anchor-boxes/
def generateAnchorBoxes(annotations, labels, num_anchor=9):
    '''Main method to generate anchor boxes for annotations input'''
    annotation_dims = []
    for anno in annotations:
        for obj in anno['object']:
            rel_width = (float(obj['xmax']) - float(obj['xmin'])) / anno['width']
            rel_height = (float(obj['ymax']) - float(obj['ymin'])) / anno['height']
            annotation_dims.append(tuple(map(float,(rel_width, rel_height))))

    anno_arr = np.array(annotation_dims)
    centroids = calculate_kMeans(anno_arr, num_anchor)
    centroids_sorted = centroids[centroids[:,0].argsort()]
    anchor_box = []
    for cent in centroids_sorted:
        anchor_box.append(int(cent[0]*416))
        anchor_box.append(int(cent[1]*416))
    return anchor_box
#-----------------------------------------
#%%
#----------Data generator with Imgaug----------
#Refering to https://keras.io/utils/ (Sequence)
class DataGenerator(Sequence):
    '''Generate data with augmentations using Keras Util Sequence API
        Required Methods:
        def __init__(self)
        def __len__(self)
        def on_epoch_end(self)
        def __getitem__(self)'''

    def __init__(self, annotations, max_boxes, anchors, labels, batch_size=16, width=416, height=416, shuffle=True):
        self.annotations=annotations #instance
        self.max_boxes=max_boxes
        self.anchors=self._init_anchors(anchors)
        self.labels=labels
        self.batch_size=batch_size
        self.shuffle=shuffle
        self.basefactor=32
        self.width=width
        self.height=height
        self.min_size=320
        self.max_size=608
        self.on_epoch_end()

    def _init_anchors(self, anchors):
        anchor_boxes = []
        for i in range(len(anchors)//2):
            a = anchors[2*i+1]
            anchor_boxes.append(BoundingBox(
                xmin=0.0,
                xmax=anchors[2*i],
                ymin=0.0,
                ymax=anchors[2*i+1]))
        return anchor_boxes

    def __len__(self):
        return int(np.ceil(float(len(self.annotations))//self.batch_size))

    def on_epoch_end(self):
        '''Update index after each epoch'''
        self.index = np.arange(len(self.annotations))
        if self.shuffle:
            np.random.shuffle(self.index)
    
    def _augmentation(self, images):
        '''Image augmentation with imgaug'''
        seq = iaa.Sequential([
            iaa.AdditiveGaussianNoise(scale=0.05*255)])
        return seq(images=images)
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      
    def _current_size(self, idx):
        '''Set the image size for the current image, change size every 10 image'''
        if idx%10 == 0:
            net_size = self.basefactor*np.random.randint(self.min_size/self.basefactor, \
                                                         self.max_size/self.basefactor+1)
            self.height, self.width = net_size, net_size
        return self.height, self.width
   
    def _limit_range(self, lowerBound, higherBound, value):
        if value < lowerBound: return lowerBound
        if value > higherBound: return higherBound
        return value

    def _multi_scale_boundingboxes(self, boxes, rescale_height, rescale_width, currHeight, currWidth, padIdx_X, padIdx_Y, img_h, img_w):
        boxes = copy.deepcopy(boxes)

        x_scale = float(rescale_width)/img_w
        y_scale = float(rescale_height)/img_h

        empty_boxes = []
        for idx in range(len(boxes)):
            boxes[idx].xmin = int(self._limit_range(0, currWidth, boxes[idx].xmin * x_scale + padIdx_X))
            boxes[idx].xmax = int(self._limit_range(0, currWidth, boxes[idx].xmax * x_scale + padIdx_X))
            boxes[idx].ymin = int(self._limit_range(0, currHeight, boxes[idx].ymin * y_scale + padIdx_Y))
            boxes[idx].ymax = int(self._limit_range(0, currHeight, boxes[idx].ymax * y_scale + padIdx_Y))

            if boxes[idx].xmax <= boxes[idx].xmin or boxes[idx].ymax <= boxes[idx].ymin:
                empty_boxes.append(idx)
                continue
        return [boxes[i] for i in range(len(boxes)) if i not in empty_boxes]

    def _multi_scale_image(self, img, bbs, currHeight, currWidth):
        '''Scale and crop images according to current batch randomize image width and height'''
        img_height, img_width, _ = img.shape
        # Randomise the scale of the input images
        random_scale = np.random.uniform(0.25, 2)
        aspect_ratio = img_width/img_height
        rescale_height = int(random_scale * currHeight) if aspect_ratio < 1 else int(currWidth / aspect_ratio)
        rescale_width = int(currHeight * aspect_ratio) if aspect_ratio < 1 else int(random_scale * currWidth)

        # Scale and crop
        padIdx_X = int(np.random.uniform(0, currWidth - rescale_width))
        padIdx_Y = int(np.random.uniform(0, currHeight - rescale_height))
        img_resized = cv2.resize(img, (rescale_width, rescale_height))

        if padIdx_X > 0:
            img_resized = np.pad(array=img_resized,
                                pad_width=((0,0),(padIdx_X,0),(0,0)),
                                constant_values=0,
                                mode='constant')
        else:
            img_resized = img_resized[:,-padIdx_X:,:]
        
        if (rescale_width + padIdx_X) < currHeight:
            img_resized = np.pad(array=img_resized,
                                pad_width=((0,0),(0,currWidth - (rescale_width + padIdx_X)),(0,0)),
                                constant_values=0,
                                mode='constant')
        
        if padIdx_Y > 0:
            img_resized = np.pad(array=img_resized,
                                pad_width=((padIdx_Y,0),(0,0),(0,0)),
                                constant_values=0,
                                mode='constant')
        else:
            img_resized = img_resized[-padIdx_Y:,:,:]

        if (rescale_height + padIdx_Y) < currHeight:
            img_resized = np.pad(array=img_resized,
                                pad_width=((0,currHeight - (rescale_height + padIdx_Y)),(0,0),(0,0)),
                                constant_values=0,
                                mode='constant')
        img_resized =  img_resized[:currHeight, :currWidth, :]
        bbs_resized = self._multi_scale_boundingboxes(bbs, rescale_height, rescale_width, currHeight, currWidth, padIdx_X, padIdx_Y, img_height, img_width)
        return img_resized, bbs_resized    

    def __getitem__(self, index):
        '''Get input per batch'''
        height, width = self._current_size(index)
        grid_height, grid_width = height//self.basefactor, width//self.basefactor
        curr_indices = index * self.batch_size
        next_indices = (index + 1) * self.batch_size
        if curr_indices > len(self.annotations):
            curr_indices = len(self.annotations)
            next_indices = curr_indices - self.batch_size
        input_images = np.zeros((next_indices - curr_indices, height, width, 3))
        groundtruths = np.zeros((next_indices - curr_indices, 1, 1, 1, self.max_boxes, 4))

        yolo_bigout = np.zeros((next_indices - curr_indices, grid_height, grid_width, len(self.anchors)//3, 5+len(self.labels)))
        yolo_midout = np.zeros((next_indices - curr_indices, 2 * grid_height, 2 * grid_width, len(self.anchors)//3, 5+len(self.labels)))
        yolo_smallout = np.zeros((next_indices - curr_indices, 4 * grid_height, 4 * grid_width, len(self.anchors)//3, 5+len(self.labels)))
        all_out = [yolo_smallout, yolo_midout, yolo_bigout]
        yolo_loss1 = np.zeros((next_indices - curr_indices, 1))
        yolo_loss2 = np.zeros((next_indices - curr_indices, 1))
        yolo_loss3 = np.zeros((next_indices - curr_indices, 1))

        true_box_idx = 0
        img_count = 0
        for anno in self.annotations[curr_indices:next_indices]: #Each image and annotations for current batch
            fileName = anno['filename']
            raw_img = cv2.imread(fileName)
            
            raw_img = cv2.cvtColor(raw_img, cv2.COLOR_BGR2RGB)
            anno_bbs = anno['bbs']
            img, bbs = self._multi_scale_image(raw_img, anno_bbs, height, width)
            img = self._augmentation(img)
            for box in bbs:
                max_anchor, max_index = self._get_best_anchor(box)
                yolo_out = all_out[max_index//3]
                yolo_grid_height, yolo_grid_width = yolo_out.shape[1:3]
                centerX = .5*(box.xmin + box.xmax)
                centerX = centerX / float(width) * yolo_grid_width
                centerY = .5*(box.ymin + box.ymax)
                centerY = centerY / float(height) * yolo_grid_height
                w = np.log((box.xmax - box.xmin) / float(max_anchor.xmax))
                h = np.log((box.ymax - box.ymin) / float(max_anchor.ymax))
                yolobox = [centerX, centerY, w, h]
                anno_idx = list(self.labels).index(box.label)
                gridX = int(np.floor(centerX))
                gridY = int(np.floor(centerY))

                yolo_out[img_count, gridY, gridX, max_index%3] = 0
                yolo_out[img_count, gridY, gridX, max_index%3, 0:4] = yolobox
                yolo_out[img_count, gridY, gridX, max_index%3, 4] = 1.
                yolo_out[img_count, gridY, gridX, max_index%3, 5+anno_idx] = 1
                true_box = [centerX, centerY, box.xmax - box.xmin, box.ymax - box.ymin]
                groundtruths[img_count, 0, 0, 0, true_box_idx] = true_box
                true_box_idx += 1
                true_box_idx = true_box_idx % self.max_boxes

            # For checking on image and boundingbox scale
            # for b in bbs:
            #     cv2.rectangle(img, (b.xmin,b.ymin), (b.xmax, b.ymax), (255,0,0),3)
            #     cv2.putText(img, b.label, 
            #                     (b.xmin+2, b.ymin+12), 
            #                     0, 1.2e-3 * img.shape[0], 
            #                     (0,255,0), 2)
            input_images[img_count] = img/255
            img_count += 1
        return [input_images, groundtruths, yolo_bigout, yolo_midout, yolo_smallout], [yolo_loss1, yolo_loss2, yolo_loss3]    

    def _get_best_anchor(self, boundbox):
        '''Compare bounding box with all anchors and find best match'''
        max_anchor = None
        max_index = -1
        max_iou = -1
        bb = BoundingBox(
            xmin=0.0,
            xmax=boundbox.xmax - boundbox.xmin,
            ymin=0.0,
            ymax=boundbox.ymax - boundbox.ymin)
        for idx in range(len(self.anchors)):
            anchor = self.anchors[idx]
            iou = calculate_bb_iou(bb, anchor)
            if max_iou < iou:
                max_anchor = anchor
                max_index = idx
                max_iou = iou
        return max_anchor, max_index

#----------------------------------------------
#%%
#----------Callback----------
def create_callbacks(filepath):
    return [
        EarlyStopping(
            monitor='loss',
            mode='min',
            min_delta=0.01,
            patience=7,
            verbose=1),
        ReduceLROnPlateau(
            monitor='loss',
            patience=7,
            verbose=1,
            mode='min',
            min_delta=0.01),
        ModelCheckpoint(
            filepath=filepath,
            monitor='loss',
            verbose=1,
            save_best_only=True,
            save_weights_only=True,
            mode='min',
            period=1)]
#----------------------------
#----------Loss Layer----------
# Refering to this https://keras.io/layers/writing-your-own-keras-layers/
class YoloLossLayer(Layer):
    '''Use Keras API for layer creation to create custom layer for loss calculation
        Required methods:
        def __init__(self)
        def build(self)
        def call(self)'''
    def __init__(self, anchors, max_grid, batch_size, threshold, **kwargs):
        self.anchors = tf.constant(anchors, dtype='float', shape=[1,1,1,3,2])
        maxgrid_h, maxgrid_w = max_grid
        cell_x = tf.to_float(tf.reshape(tf.tile(tf.range(maxgrid_w), [maxgrid_h]), (1, maxgrid_h, maxgrid_w, 1, 1)))
        cell_y = tf.transpose(cell_x, (0,2,1,3,4))
        self.cell_grid = tf.tile(tf.concat([cell_x,cell_y],-1), [batch_size, 1, 1, 3, 1])
        self.threshold = threshold
        super(YoloLossLayer, self).__init__(**kwargs)
    
    def build(self, input_shape):
        super(YoloLossLayer, self).build(input_shape)
    
    def compute_output_shape(self, input_shape):
        return [(None, 1)]
    
    def call(self, x):
        input_img, y_pred, y_true, true_boxes = x

        y_pred = tf.reshape(y_pred, tf.concat([tf.shape(y_pred)[:3], tf.constant([3, -1])], axis=0))
        object_mask     = tf.expand_dims(y_true[..., 4], 4)
        
        grid_h      = tf.shape(y_true)[1]
        grid_w      = tf.shape(y_true)[2]
        grid_factor = tf.reshape(tf.cast([grid_w, grid_h], tf.float32), [1,1,1,1,2])

        net_h       = tf.shape(input_img)[1]
        net_w       = tf.shape(input_img)[2]            
        net_factor  = tf.reshape(tf.cast([net_w, net_h], tf.float32), [1,1,1,1,2])
        pred_box_xy    = (self.cell_grid[:,:grid_h,:grid_w,:,:] + tf.sigmoid(y_pred[..., :2]))  
        pred_box_wh    = y_pred[..., 2:4]                                                       
        pred_box_conf  = tf.expand_dims(tf.sigmoid(y_pred[..., 4]), 4)                          
        pred_box_class = y_pred[..., 5:]                  
        true_box_xy    = y_true[..., 0:2]
        true_box_wh    = y_true[..., 2:4]
        true_box_conf  = tf.expand_dims(y_true[..., 4], 4)
        true_box_class = tf.argmax(y_true[..., 5:], -1)   
        conf_delta  = pred_box_conf - 0 
        true_xy = true_boxes[..., 0:2] / grid_factor
        true_wh = true_boxes[..., 2:4] / net_factor
        
        true_wh_half = true_wh / 2.
        true_mins    = true_xy - true_wh_half
        true_maxes   = true_xy + true_wh_half
        
        pred_xy = tf.expand_dims(pred_box_xy / grid_factor, 4)
        pred_wh = tf.expand_dims(tf.exp(pred_box_wh) * self.anchors / net_factor, 4)
        
        pred_wh_half = pred_wh / 2.
        pred_mins    = pred_xy - pred_wh_half
        pred_maxes   = pred_xy + pred_wh_half    

        intersect_mins  = tf.maximum(pred_mins,  true_mins)
        intersect_maxes = tf.minimum(pred_maxes, true_maxes)

        intersect_wh    = tf.maximum(intersect_maxes - intersect_mins, 0.)
        intersect_areas = intersect_wh[..., 0] * intersect_wh[..., 1]
        
        true_areas = true_wh[..., 0] * true_wh[..., 1]
        pred_areas = pred_wh[..., 0] * pred_wh[..., 1]

        union_areas = pred_areas + true_areas - intersect_areas
        iou_scores  = tf.truediv(intersect_areas, union_areas)

        best_ious   = tf.reduce_max(iou_scores, axis=4)        
        conf_delta *= tf.expand_dims(tf.to_float(best_ious < self.threshold), 4)

        wh_scale = tf.exp(true_box_wh) * self.anchors / net_factor
        wh_scale = tf.expand_dims(2 - wh_scale[..., 0] * wh_scale[..., 1], axis=4)

        xy_delta    = object_mask   * (pred_box_xy-true_box_xy) * wh_scale
        wh_delta    = object_mask   * (pred_box_wh-true_box_wh) * wh_scale
        conf_delta  = object_mask * (pred_box_conf-true_box_conf) * 5 + (1-object_mask) * conf_delta
        class_delta = object_mask * \
                      tf.expand_dims(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=true_box_class, logits=pred_box_class), 4) #regression

        loss_xy    = tf.reduce_sum(tf.square(xy_delta), list(range(1,5)))
        loss_wh    = tf.reduce_sum(tf.square(wh_delta), list(range(1,5)))
        loss_conf  = tf.reduce_sum(tf.square(conf_delta), list(range(1,5)))
        loss_class = tf.reduce_sum(class_delta, list(range(1,5)))
        return loss_xy + loss_wh + loss_conf + loss_class 
#------------------------------
#%%
#---------- YOLOv3 Model----------
def createYoloLyr(inputs, convLyrs, skip=True):
    '''Create yolo layer'''
    x = inputs
    loopCounter = 0
    for convLyr in convLyrs:
        if loopCounter == (len(convLyrs)-2) and skip:
            skip_connection = x
        loopCounter += 1
        if convLyr['strides'] > 1: 
            x = ZeroPadding2D(((1,0),(1,0)))(x)
        x = Conv2D(filters=convLyr['filters'], 
                   kernel_size=convLyr['kernel_size'], 
                   strides=convLyr['strides'], 
                   padding='valid' if convLyr['strides']> 1 else 'same',  
                   use_bias=False if convLyr['bnorm'] else True,
                   kernel_regularizer=regularizers.l2(0.0001))(x)
        if convLyr['bnorm']:
            x = BatchNormalization(epsilon=0.001)(x)
        if convLyr['leakyRelu']:
            x = LeakyReLU(alpha=0.1)(x)
    return add([skip_connection, x]) if skip else x

def YoloV3(numcls,anchors, max_grid, batch_size, threshold, max_boxes):
    '''Main YOLOv3 Model'''
    img = Input(shape=(None,None,3))
    true_bboxes = Input(shape=(1,1,1,max_boxes, 4))
    true_bbox_1 = Input(shape=(None, None, len(anchors)//6, 5+numcls))
    true_bbox_2 = Input(shape=(None, None, len(anchors)//6, 5+numcls))
    true_bbox_3 = Input(shape=(None, None, len(anchors)//6, 5+numcls))
    x = createYoloLyr(img, [
        {'filters': 32, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 64, 'kernel_size': 3, 'strides': 2, 'bnorm': True, 'leakyRelu': True},
        {'filters': 32, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 64, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    x = createYoloLyr(x, [
        {'filters': 128, 'kernel_size': 3, 'strides': 2, 'bnorm': True, 'leakyRelu': True},
        {'filters': 64, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 128, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    x = createYoloLyr(x, [
        {'filters': 64, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 128, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    x = createYoloLyr(x, [
        {'filters': 256, 'kernel_size': 3, 'strides': 2, 'bnorm': True, 'leakyRelu': True},
        {'filters': 128, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 256, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    for _ in range(7):
        x = createYoloLyr(x, [
            {'filters': 128, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
            {'filters': 256, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    out1 = x
    x = createYoloLyr(x, [
        {'filters': 512, 'kernel_size': 3, 'strides': 2, 'bnorm': True, 'leakyRelu': True},
        {'filters': 256, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 512, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    for _ in range(7):
        x = createYoloLyr(x, [
            {'filters': 256, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
            {'filters': 512, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    out2 = x
    x = createYoloLyr(x, [
        {'filters': 1024, 'kernel_size': 3, 'strides': 2, 'bnorm': True, 'leakyRelu': True},
        {'filters': 512, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 1024, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    for _ in range(3):
        x = createYoloLyr(x, [
            {'filters': 512, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
            {'filters': 1024, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True}])
    x = createYoloLyr(x, [
        {'filters': 512, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 1024, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 512, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 1024, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 512, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True}], skip=False)
    bigPred = createYoloLyr(x, [
        {'filters': 1024, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': (3*(5 + numcls)), 'kernel_size': 1, 'strides': 1, 'bnorm': False, 'leakyRelu': False}], skip=False)
    loss_big = YoloLossLayer(anchors=anchors[12:],
                                max_grid=[1*num for num in max_grid],
                                batch_size=batch_size,
                                threshold=threshold)([img, bigPred, true_bbox_1, true_bboxes])
    x = createYoloLyr(x, [{'filters': 256, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True}], skip=False)
    x = UpSampling2D(2)(x)
    x = concatenate([x, out2])
    x = createYoloLyr(x, [
        {'filters': 256, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 512, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 256, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 512, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 256, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True}], skip=False)
    midPred = createYoloLyr(x, [
        {'filters': 512, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': (3*(5 + numcls)), 'kernel_size': 1, 'strides': 1, 'bnorm': False, 'leakyRelu': False}], skip=False)
    loss_mid = YoloLossLayer(anchors=anchors[6:12],
                            max_grid=[2*num for num in max_grid],
                            batch_size=batch_size,
                            threshold=threshold)([img, midPred, true_bbox_2, true_bboxes])
    x = createYoloLyr(x, [
        {'filters': 128, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True}], skip=False)
    x = UpSampling2D(2)(x)
    x = concatenate([x, out1])
    smallPred = createYoloLyr(x, [
        {'filters': 128, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 256, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 128, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 256, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 128, 'kernel_size': 1, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': 256, 'kernel_size': 3, 'strides': 1, 'bnorm': True, 'leakyRelu': True},
        {'filters': (3*(5+ numcls)), 'kernel_size': 1, 'strides': 1, 'bnorm': False, 'leakyRelu': False}], skip=False)
    loss_small = YoloLossLayer(anchors=anchors[:6],
                            max_grid=[4*num for num in max_grid],
                            batch_size=batch_size,
                            threshold=threshold)([img, smallPred, true_bbox_3, true_bboxes])
    trainModel = Model([img, true_bboxes, true_bbox_1, true_bbox_2, true_bbox_3], [loss_big, loss_mid, loss_small])
    inferModel = Model(img, [bigPred, midPred, smallPred])
    return [trainModel, inferModel]
#---------------------------------

