import cv2
import os
import ast
import tqdm
import math
import random
import numpy as np
import pickle
import pandas as pd
from PIL import Image
import torch.utils.data as data
from torchvision import transforms
from sklearn.model_selection import StratifiedKFold
from skimage.io import imread
from multiprocessing import Pool

cv2.setNumThreads(0)

class OiDataset(data.Dataset):
    def __init__(self,
                 mode = 'train', # 'train' or val'
                 random_state = 42,
                 fold = 0,
                 size_ratio = 1.0,
                 
                 mean = (0.485, 0.456, 0.406),
                 std = (0.229, 0.224, 0.225),
                 
                 weight_log_base = 2,
                 min_class_weight = 1,
                 img_size_cluster = 0,
                 
                 data_folder = '../../../hdd/open_images/',
                 # train_imgs_folder = '../../../hdd/open_images/train/',
                 # val_imgs_folder = '../../../hdd/open_images/train/',
                 # train_imgs_folder = '../data/train/train/',
                 train_imgs_folder = '../data/train/train/',
                 
                 
                 val_imgs_folder = '../data/train/train/',                 
                 
                 label_list_path = '../data/label_list',
                 label_counts_path = '../data/label_counts.csv',
                 e2e_resize_dict_path = '../data/e2e_resize_dict.pickle',
                 imgid_size_dict_path = '../data/imgid_size.pickle',
                 # multi_label_dataset_path = '../data/multi_label_imgs_area_classes.csv',
                 multi_label_dataset_path = '../data/multi_label_imgs_class_count_corrected_relatons_ohe.csv',
                  
                 return_img_id = False,
                 augs = False,
                 stratify_label = 'class_count',
                 prob = 0.25,
                 oversampling_floor = 8,
                 oversampling = False
                 
                ):
        
        self.fold = fold
        self.mode = mode
        self.mean = mean
        self.std = std
        self.size_ratio = size_ratio
        self.random_state = random_state
        self.weight_log_base = weight_log_base
        self.min_class_weight = min_class_weight
        
        self.return_img_id = return_img_id
        self.augs = augs
        
        self.prob = prob
        self.oversampling_floor = oversampling_floor
        
        cluster_dict = {
            0:'(512, 1024)',
            1:'(1024, 512)',
            2:'(768, 768)'
        }
        
        self.data_folder = data_folder
        self.train_imgs_folder = train_imgs_folder
        self.val_imgs_folder = val_imgs_folder
        
        with open(label_list_path, 'rb') as handle:
            self.label_list = pickle.load(handle)

        with open(e2e_resize_dict_path, 'rb') as handle:
            self.e2e_resize_dict = pickle.load(handle)
            
        with open(imgid_size_dict_path, 'rb') as handle:
            self.imgid_size_dict = pickle.load(handle)
            
        self.label_counts = pd.read_csv(label_counts_path, names=['class','count'])
        multi_label_dataset = pd.read_csv(multi_label_dataset_path)

        if img_size_cluster != 'sample':
            # choose only images of one size
            multi_label_dataset = multi_label_dataset[multi_label_dataset.target_resl == cluster_dict[img_size_cluster]]
       
        self.ohe_values = list(multi_label_dataset.ohe_vectors.values)
        self.stratify_values = list((multi_label_dataset[stratify_label]).astype('int').values)
        self.img_ids = list(multi_label_dataset['img_id'].values)
        
        skf = StratifiedKFold(n_splits=5,
                              shuffle = True,
                              random_state = self.random_state)
        
        f1, f2, f3, f4, f5 = skf.split(self.img_ids,
                                       self.stratify_values)
        
        folds = [f1, f2, f3, f4, f5]
        if self.mode == 'train':
            self.train_idx = list(folds[self.fold][0])
            train_idx_dict = dict(zip(self.train_idx,
                                      range(0,len(self.train_idx))))            
         
            if img_size_cluster == 'sample':
                # save indexes of each cluster
                # to be used later in sampling process
                # leave only the train indexes
                cluster_indices = []
                for cluster,img_size in cluster_dict.items():
                    # leave only the train indexes
                    condition = (multi_label_dataset.target_resl == img_size)&(multi_label_dataset.index.isin(self.train_idx))
                    cluster_list = list(multi_label_dataset[condition].index.values)
                    # reindex the cluster indices with respect to the train/val split values
                    cluster_list = [train_idx_dict[_]  for _ in cluster_list]
                    cluster_indices.append(cluster_list)                    

                self.cluster_indices = cluster_indices
        elif self.mode == 'val':
            self.val_idx = list(folds[self.fold][1])
            val_idx_dict = dict(zip(self.val_idx,
                                    range(0,len(self.val_idx))))
            
            if img_size_cluster == 'sample':
                # save indexes of each cluster
                # to be used later in sampling process
                # leave only the train indexes
                cluster_indices = []
                for cluster,img_size in cluster_dict.items():
                    # leave only the train indexes
                    condition = (multi_label_dataset.target_resl == img_size)&(multi_label_dataset.index.isin(self.val_idx))
                    cluster_list = list(multi_label_dataset[condition].index.values)
                    # reindex the cluster indices with respect to the train/val split values
                    cluster_list = [val_idx_dict[_]  for _ in cluster_list]
                    cluster_indices.append(cluster_list)
                    
                self.cluster_indices = cluster_indices            
        
        del multi_label_dataset
        self.produce_weights()
        
        if oversampling:
            # use this later with a sampler
            self.dataset_oversampling_list = self.produce_oversampling_weights()
            
            if self.mode == 'train':
                # first we need to revert the train/val indexing
                # then we need to pull the necessary oversampling values
                train_idx_dict_reverse = dict(zip(range(0,len(self.train_idx)),
                                                  self.train_idx)) 
                self.oversampling_indices = []
                for cluster in self.cluster_indices:
                    self.oversampling_indices.append([self.dataset_oversampling_list[train_idx_dict_reverse[_]] for _ in cluster])
    def __len__(self):
        if self.mode == 'train':
            return len(self.train_idx)
        elif self.mode == 'val':
            return len(self.val_idx)
    def produce_weights(self):
        max_log = self.label_counts['count'].apply(lambda x: math.log(x, self.weight_log_base)).max()
        self.label_counts['class_weight'] = self.label_counts['count'].apply(lambda x: self.min_class_weight+(max_log-math.log(x, self.weight_log_base)))

        label_weight_dict = dict(zip(self.label_counts['class'].values,self.label_counts['class_weight'].values))
        self.label_weight_list = np.asarray([label_weight_dict[_] for _ in self.label_list])
    def produce_oversampling_weights(self):
        # classes will be oversampled only if their log count is lower than specified
        oversampling_list = [int(math.ceil(max(_-self.oversampling_floor,1))**1.5) for _ in self.label_weight_list]
        
        print('Calculating oversampling weights - it is slow due to ast literal eval')
        with Pool(6) as p:
            ohe_lists = list(tqdm.tqdm(p.imap(leval, self.ohe_values), total=len(self.ohe_values)))

        def produce_oversampling_factor(ohe,oversampling_list):
            factors = []
            for i,_ in enumerate(ohe):
                if _== 1:
                    factors.append(oversampling_list[i])
            return max(factors)              
            
        dataset_oversampling_list = [produce_oversampling_factor(_, oversampling_list) for _ in ohe_lists]
        return dataset_oversampling_list
    def __getitem__(self, idx):
        if self.mode == 'train':
            img_id = self.img_ids[self.train_idx[idx]]
            img_path = os.path.join(self.train_imgs_folder,img_id)+'.jpg'
            ohe_values = self.ohe_values[self.train_idx[idx]]
        elif self.mode == 'val':
            img_id = self.img_ids[self.val_idx[idx]]
            img_path = os.path.join(self.val_imgs_folder,img_id)+'.jpg'
            ohe_values = self.ohe_values[self.val_idx[idx]]
        
        ohe_values = np.asarray(ast.literal_eval(ohe_values))
        target_size = self.e2e_resize_dict[self.imgid_size_dict[img_id]]
        img = self.preprocess_img(img_path,target_size)
        
        if img is None:
            # do not return anything
            pass
        else:
            # add failsafe values here
            
            if self.return_img_id == False:
                return_tuple = (img,
                                ohe_values,
                                self.label_weight_list)                
            else:
                return_tuple = (img,
                                ohe_values,
                                self.label_weight_list,
                                img_id)
            return return_tuple 
    def preprocess_img(self,
                       img_path,
                       target_size,
                       ):


        final_size =  [int(_ * self.size_ratio) for _ in target_size]
        img = imread(img_path)

        # gray-scale img
        if len(img.shape)==2:
            # convert grayscale images to RGB
            img = cv2.cvtColor(img,cv2.COLOR_GRAY2RGB)
        # gif image
        elif len(img.shape)==1:
            img = img[0]
        # alpha channel image
        elif img.shape[2] == 4:
            img = img[:,:,0:3]

        img = Image.fromarray(img)

        if self.augs == False:
            preprocessing = transforms.Compose([
                            transforms.Resize(final_size),                
                            transforms.ToTensor(),
                            transforms.Normalize(mean=self.mean,
                                                 std=self.std),
                            ])
        else:
            add_transforms = [transforms.ColorJitter(brightness=0.1, contrast=0.1, saturation=0.1, hue=0.1),
                              RandomResizedCropRect(final_size,scale=(0.8, 1.0), ratio=(0.8, 1.2), interpolation=2),
                              ]

            preprocessing = transforms.Compose([
                            transforms.Resize(final_size),

                            transforms.RandomApply(add_transforms, p=self.prob),
                            transforms.RandomHorizontalFlip(p=self.prob),
                            transforms.RandomVerticalFlip(p=self.prob),
                            transforms.RandomGrayscale(p=self.prob),

                            transforms.ToTensor(),
                            transforms.Normalize(mean=self.mean,
                                                 std=self.std),
                            ])                
        img_arr = preprocessing(img).numpy()
        return img_arr
    
class RandomResizedCropRect(transforms.RandomResizedCrop):
    """Extend the PyTorch function so that it could accept non-square images
    """    
    def __init__(self, size, scale=(0.08, 1.0), ratio=(3. / 4., 4. / 3.), interpolation=Image.BILINEAR):
        super(RandomResizedCropRect, self).__init__(size[0], scale, ratio, interpolation)
        self.size = size
        
def leval(x):
    return ast.literal_eval(x)      