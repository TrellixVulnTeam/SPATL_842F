import os
import shutil
import torch
import torch.nn as nn
import sys
sys.path.append("./graph_env")
from pruning_head.graph_env.graph_construction import hierarchical_graph_construction, net_info
from pruning_head.graph_env.feedback_calculation import reward_caculation
from pruning_head.graph_env.flops_calculation import flops_caculation_forward, preserve_flops
from pruning_head.graph_env.share_layers import share_layer_index
from pruning_head.graph_env.network_pruning import channel_pruning


import numpy as np
import copy


class graph_env:

    def __init__(self,model,n_layer,dataset,val_loader,compression_ratio,g_in_size,log_dir,input_x,device,args):
        #work space
        self.args = args
        self.log_dir = log_dir
        self.device = device

        #DNN
        self.model = model
        self.model_name = args.model
        self.pruned_model = None
        self.best_pruned_model = None
        #self.pruning_index = pruning_index
        self.input_x = input_x
        self.pruned_model_flops = None
        self.flops, self.flops_share = flops_caculation_forward(self.model, args.model, input_x, preserve_ratio=None)
        self.total_flops = sum(self.flops)
        # self.in_channels,self.out_channels,self.n_blocks = get_channels(args.model)
        self.in_channels,self.out_channels,_ = net_info(args.model)
        self.sparsity = 1.0

        self.preserve_in_c = copy.deepcopy(self.in_channels)
        self.preserve_out_c = copy.deepcopy(self.out_channels)
        self.pruned_out_c = None
        self.n_layer = n_layer
        #dataset
        self.dataset = dataset
        self.val_loader = val_loader

        #pruning
        self.desired_flops = self.total_flops * compression_ratio
        self.preserve_ratio = torch.ones([n_layer])
        self.best_accuracy = 0

        #graph
        self.g_in_size = g_in_size
        self.current_states = None
        #env
        self.done = False
        self.max_timesteps = args.max_timesteps
        _, accuracy,_,_ = reward_caculation(self.args, self.model, self.val_loader, root=self.log_dir)
        print("Initial val. accuracy:",accuracy)


    def reset(self):
        self.done=False
        self.pruned_model = None
        self.preserve_ratio = torch.ones([self.n_layer])
        self.current_states = self.model_to_graph()
        self.preserve_in_c = copy.deepcopy(self.in_channels)
        self.preserve_out_c = copy.deepcopy(self.out_channels)
        self.pruned_out_c = None

        return self.current_states

    def step(self,actions,time_step):

        rewards = 0
        accuracy = 0
        # self.preserve_ratio *= 1 - np.array(share_layer_index(self.model,actions,self.args.model)).astype(float)
        self.preserve_ratio *= 1 - np.array(actions).astype(float)
        if self.model_name in ['mobilenet','mobilenetv2']:
            self.preserve_ratio = np.clip(self.preserve_ratio, 0.9, 1)
        else:
            self.preserve_ratio = np.clip(self.preserve_ratio, 0.1, 1)
        # self.preserve_ratio = np.clip(self.preserve_ratio, 0.1, 0.98)

        #pruning the model
        # self.preserve_ratio[0] = 1
        # self.preserve_ratio[-1] = 1
        self.pruned_channels()

        current_flops = preserve_flops(self.flops,self.preserve_ratio,self.model_name,actions)
        reduced_flops = self.total_flops - sum(current_flops)

        #desired flops reduction

        if reduced_flops >= self.desired_flops:
            r_flops = 1 - reduced_flops/self.total_flops
            # print("FLOPS ratio:",r_flops)
            self.done = True
            self.pruned_model = channel_pruning(self.model,self.preserve_ratio)
            rewards, accuracy,_,_ = reward_caculation(self.args, self.pruned_model, self.val_loader, root=self.log_dir)

            '''
            if self.dataset == "cifar10":
                rewards = -100
                for i in range(10):
                    pruned_model = channel_pruning(self.model,self.preserve_ratio)
                    r, acc,_,_ = reward_caculation(self.args, pruned_model, self.val_loader, root=self.log_dir)
                    if r > rewards:
                        rewards = r
                        accuracy = acc
                        self.pruned_model = pruned_model
            else:
                self.pruned_model = channel_pruning(self.model,self.preserve_ratio)
                _,_,rewards, accuracy = reward_caculation(self.args, self.pruned_model, self.val_loader, root=self.log_dir)
            '''
            if accuracy > self.best_accuracy:
                self.best_accuracy = accuracy
                self.best_pruned_model = copy.deepcopy(self.pruned_model)
                self.pruned_model_flops = r_flops
                # self.save_checkpoint({
                #     'model': self.args.model,
                #     'dataset': self.dataset,
                #     'preserve_ratio':self.preserve_ratio,
                #     'state_dict': self.pruned_model.module.state_dict() if isinstance(self.pruned_model, nn.DataParallel) else self.pruned_model.state_dict(),
                #     'acc': self.best_accuracy,
                #     'flops':r_flops
                # }, True, checkpoint_dir=self.log_dir)
                sparsity = self.caculate_sparcity_ratio()
                self.sparsity = sparsity
                print("Best Accuracy (without fine-tuning) of Compressed Models: {}. The FLOPs ratio: {}".format( self.best_accuracy,r_flops))
                print("The sparcity ratio of the salient weights:", sparsity)

        if time_step == (self.max_timesteps):
            if not self.done:
                rewards = -100
                self.done = True
        graph = self.model_to_graph()
        return graph,rewards,self.done


    def pruned_channels(self):
        self.preserve_in_c = copy.deepcopy(self.in_channels)
        self.preserve_in_c[1:] = (self.preserve_in_c[1:]*np.array(self.preserve_ratio[:-1]).reshape(-1)).astype(int)

        self.preserve_out_c = copy.deepcopy(self.out_channels)
        self.preserve_out_c = (self.preserve_out_c*np.array(self.preserve_ratio).reshape(-1)).astype(int)
        self.pruned_out_c = self.out_channels - self.preserve_out_c

    def model_to_graph(self):
        graph = hierarchical_graph_construction(self.preserve_in_c,self.preserve_out_c,self.model_name,self.g_in_size,self.device)
        return graph


    def save_checkpoint(self,state, is_best, checkpoint_dir='.'):
        filename = os.path.join(checkpoint_dir, self.args.model+'ckpt.pth.tar')
        print('=> Saving checkpoint to {}'.format(filename))
        torch.save(state, filename)
        if is_best:
            shutil.copyfile(filename, filename.replace('.pth.tar', '.best.pth.tar'))

    def get_pruned_model(self):
        if self.best_pruned_model is None:
            self.best_pruned_model = channel_pruning(self.model,self.preserve_ratio)

        return self.best_pruned_model, self.pruned_model_flops, self.sparsity

    def caculate_sparcity_ratio(self):
        total_weights = 0
        non_zero_weights = 0
        i=0
        for name, module in self.model.named_modules():
            if isinstance(module, nn.Conv2d):
                num_layer_weights = float(module.weight.detach().cpu().numpy().size)
                total_weights += num_layer_weights
                non_zero_weights += self.preserve_ratio[i] * num_layer_weights
                i+=1

        return non_zero_weights/total_weights