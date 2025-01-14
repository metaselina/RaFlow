#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import os
import torch
import copy
import random
from tqdm import tqdm
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import StepLR
from utils import *
from data import *
from models import *
import numpy as np
from losses import *
import argparse
from matplotlib import pyplot as plt
from main_util import train_one_epoch, plot_loss_epoch
from main_util import eval_scene_flow, eval_motion_seg
from vis_util import *


class IOStream:
    def __init__(self, path):
        self.f = open(path, 'a')

    def cprint(self, text):
        print(text)
        self.f.write(text + '\n')
        self.f.flush()

    def close(self):
        self.f.close()


def _init_(args):
    if not os.path.exists('checkpoints'):
        os.makedirs('checkpoints')
    if not os.path.exists('checkpoints/' + args.exp_name):
        os.makedirs('checkpoints/' + args.exp_name)
    if not os.path.exists('checkpoints/' + args.exp_name + '/' + 'models'):
         os.makedirs('checkpoints/' + args.exp_name + '/' + 'models')
    os.system('cp main.py checkpoints' + '/' + args.exp_name + '/' + 'main.py.backup')
    os.system('cp data.py checkpoints' + '/' + args.exp_name + '/' + 'data.py.backup')

        
def test(args, net, test_loader, textio):

    
    net.eval() 
    num_pcs=0 
    
    vis_path_2D='checkpoints/'+args.exp_name+"/test_vis_2d/"
    if not os.path.exists(vis_path_2D):
        os.makedirs(vis_path_2D)

    sf_metric = {'rne':0, '50-50 rne': 0, 'mov_rne': 0, 'stat_rne': 0,\
                 'sas': 0, 'ras': 0, 'epe': 0}
    seg_metric = {'acc': 0, 'miou': 0, 'sen': 0}
   

    for i, data in tqdm(enumerate(test_loader), total = len(test_loader)):
       
        pc1, pc2, ft1, ft2, _, gt , mask, interval= data
        pc1 = pc1.cuda().transpose(2,1).contiguous()
        pc2 = pc2.cuda().transpose(2,1).contiguous()
        ft1 = ft1.cuda().transpose(2,1).contiguous()
        ft2 = ft2.cuda().transpose(2,1).contiguous()
        mask = mask.cuda()
        interval = interval.cuda().float()
        gt = gt.cuda().float()
        batch_size = pc1.size(0)
 
        with torch.no_grad():

            if args.model=='raflow' or args.model == 'raflow_vod':
                _, pred_f, _, pred_m = net(pc1, pc2, ft1, ft2, interval)

            ## use estimated scene to warp point cloud 1 
            pc1_warp=pc1+pred_f
            
            ## Viualize the estimated scene flow
            if args.vis:
                visulize_result_2D(pc1, pc2, pc1_warp, num_pcs, vis_path_2D)
                
            ## evaluate the estimated results using ground truth
            batch_res = eval_scene_flow(pc1, pred_f.transpose(2,1).contiguous(), gt, mask, args)
            
            for metric in batch_res:
                sf_metric[metric] += batch_size * batch_res[metric]
                
            ## evaluate the motion segmentation precision and recall
            if args.model=='raflow' or args.model == 'raflow_vod':
                seg_res = eval_motion_seg(pred_m, mask)
                for metric in seg_res:
                    seg_metric[metric] += batch_size * seg_res[metric]
                
            num_pcs+=batch_size
            

    ## print scene flow evaluation results
    for metric in sf_metric:
        textio.cprint('###The mean {}: {}###'.format(metric, sf_metric[metric]/num_pcs))
    if args.model=='raflow' or args.model == 'raflow_vod':
        ## print motion seg evaluation results
        for metric in seg_metric:
            textio.cprint('###The mean {}: {}###'.format(metric, seg_metric[metric]/num_pcs))
   

def train(args, net, train_loader, val_loader, textio):
    
    
    opt = optim.Adam(net.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = StepLR(opt, args.decay_epochs, gamma = args.decay_rate)

    best_val_loss = np.inf
    train_loss_ls = np.zeros((args.epochs))
    val_loss_ls = np.zeros((args.epochs))
    
    train_items_iter = {
                    'Loss': [], 'smoothnessLoss': [],'veloLoss': [], 'chamferLoss': []}
    val_items_iter = copy.deepcopy(train_items_iter)
    
    for epoch in range(args.epochs):
        
        textio.cprint('==epoch: %d, learning rate: %f=='%(epoch, opt.param_groups[0]['lr']))
        total_loss, loss_items = train_one_epoch(args, net, train_loader, opt, 'train')
        train_loss_ls[epoch] = total_loss
        for it in loss_items:
            train_items_iter[it].extend([loss_items[it]])
        textio.cprint('mean train loss: %f'%total_loss)

        total_loss, loss_items = train_one_epoch(args, net, val_loader, opt, 'val')
        val_loss_ls[epoch] = total_loss
        for it in loss_items:
            val_items_iter[it].extend([loss_items[it]])
        textio.cprint('mean val loss: %f'%total_loss)
        
        if best_val_loss >= total_loss:
            best_val_loss = total_loss
            textio.cprint('best val loss till now: %f'%total_loss)
            if torch.cuda.device_count() > 1:
                torch.save(net.module.state_dict(), 'checkpoints/%s/models/model.best.t7' % args.exp_name)
            else:
                torch.save(net.state_dict(), 'checkpoints/%s/models/model.best.t7' % args.exp_name)
        
        scheduler.step()
        
        plot_loss_epoch(train_items_iter, args, epoch)
        
    plt.clf()
    plt.plot(train_loss_ls[0:int(args.epochs)], 'b')
    plt.plot(val_loss_ls[0:int(args.epochs)], 'r')
    plt.legend(['train','val'])
    plt.xlabel('epoch')
    plt.ylabel('loss')
    plt.savefig('checkpoints/%s/loss.png' % args.exp_name,dpi=500)
    

def main(io_args):
    
    args = parse_args_from_yaml("configs.yaml")
    args.eval = io_args.eval
    args.vis = io_args.vis
    args.dataset_path = io_args.dataset_path
    args.exp_name = io_args.exp_name
    args.model = io_args.model
    args.dataset = io_args.dataset

    # CUDA settings
    torch.cuda.empty_cache()
    os.environ['CUDA_VISIBLE_DEVICES'] = args.cuda_device
    
    # deterministic results
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    # init checkpoint records 
    _init_(args)
    textio = IOStream('checkpoints/' + args.exp_name + '/run.log')
    textio.cprint(str(args))

    # init dataset and dataloader
    if args.dataset=='saicDataset':
        if args.eval:
            test_loader = DataLoader(saicDataset(args=args, textio = textio, root = args.dataset_path, partition='test'),
                    num_workers=args.num_workers,batch_size=1, shuffle=False, drop_last=False)
        else:
            train_loader = DataLoader(saicDataset(args=args, textio = textio, root = args.dataset_path, partition='train'),
                    num_workers=args.num_workers, batch_size=args.batch_size, shuffle=True, drop_last=True)
            val_loader = DataLoader(saicDataset(args=args, textio = textio, root = args.dataset_path, partition='val'),
                    num_workers=args.num_workers,batch_size=args.val_batch_size, shuffle=False, drop_last=False)
                    
    if args.dataset == 'vodDataset':
        if args.eval:
            test_loader = DataLoader(vodDataset(args=args, textio = textio, root = args.dataset_path, partition='test'),
                    num_workers=args.num_workers,batch_size=1, shuffle=False, drop_last=False)
        else:
            train_loader = DataLoader(vodDataset(args=args, textio = textio, root = args.dataset_path, partition='train'),
                    num_workers=args.num_workers, batch_size=args.batch_size, shuffle=True, drop_last=True)
            val_loader = DataLoader(vodDataset(args=args, textio = textio, root = args.dataset_path, partition='val'),
                    num_workers=args.num_workers,batch_size=args.val_batch_size, shuffle=False, drop_last=False)

    # init the network (load or from scratch)
    net = init_model(args)
        
    if args.eval:
        test(args, net, test_loader, textio)
    else:
        train(args, net, train_loader, val_loader, textio)

 

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Radar Scene flow running')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--vis', action = 'store_true')
    parser.add_argument('--dataset_path', type= str, default = 'demo_data/')
    parser.add_argument('--exp_name', type = str, default = 'raflow')
    parser.add_argument('--model', type = str, default = 'raflow')
    parser.add_argument('--dataset', type = str, default = 'saicDataset')
    args = parser.parse_args()
   
    main(args)
