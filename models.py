import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as nnf
from losses import *
import logging

class SpatialTransformer(nn.Module):
    def __init__(self, size, mode='bilinear'):
        super(SpatialTransformer, self).__init__()
        zero = torch.cat([torch.eye(3), torch.zeros([3,1])], 1)[None]
        self.meshgrid = nn.Parameter(nnf.affine_grid(zero, [1,1,*size], align_corners=False), requires_grad=False)
        self.mode = mode

    def forward(self, src, flow):
        flow = flow.permute(0, 2, 3, 4, 1)
        new_locs = self.meshgrid + flow
        self.new_locs = new_locs
        return nnf.grid_sample(src, new_locs, mode=self.mode, align_corners=False)

class conv_block(nn.Module):
    """
    [conv_block] represents a single convolution block in the Unet which
    is a convolution based on the size of the input channel and output
    channels and then preforms a Leaky Relu with parameter 0.2.
    """
    def __init__(self, dim, in_channels, out_channels, stride=1):
        """
        Instiatiate the conv block
            :param dim: number of dimensions of the input
            :param in_channels: number of input channels
            :param out_channels: number of output channels
            :param stride: stride of the convolution
        """
        super(conv_block, self).__init__()

        conv_fn = getattr(nn, "Conv{0}d".format(dim))

        if stride == 1:
            ksize = 3
        elif stride == 2:
            ksize = 3
        else:
            raise Exception('stride must be 1 or 2')

        self.main = conv_fn(in_channels, out_channels, ksize, stride, 1)
        self.activation = nn.LeakyReLU(0.2)

    def forward(self, x):
        """
        Pass the input through the conv_block
        """
        out = self.main(x)
        out = self.activation(out)
        return out

class unet_core(nn.Module):
    """
    [unet_core] is a class representing the U-Net implementation that takes in
    a fixed image and a moving image and outputs a flow-field
    """
    def __init__(self, dim=3, enc_nf=[16, 32, 32, 32], dec_nf= [32, 32, 32, 32, 32, 16, 16], droprate=0, input_ch=2):
        """
        Instiatiate UNet model
            :param dim: dimension of the image passed into the net
            :param enc_nf: the number of features maps in each layer of encoding stage
            :param dec_nf: the number of features maps in each layer of decoding stage
            :param full_size: boolean value representing whether full amount of decoding 
                            layers
        """
        super(unet_core, self).__init__()

        # Encoder functions
        self.enc = nn.ModuleList()
        for i in range(len(enc_nf)):
            prev_nf = input_ch if i == 0 else enc_nf[i-1]
            self.enc.append(conv_block(dim, prev_nf, enc_nf[i], 2))
            self.enc.append(nn.Dropout(0))
        # self.drop = nn.Dropout(droprate)

        # Decoder functions
        self.dec = nn.ModuleList()
        self.dec.append(conv_block(dim, enc_nf[-1], dec_nf[0]))  # 1
        self.dec.append(nn.Dropout(0))
        self.dec.append(conv_block(dim, dec_nf[0] * 2, dec_nf[1]))  # 2
        self.dec.append(nn.Dropout(0))
        self.dec.append(conv_block(dim, dec_nf[1] * 2, dec_nf[2]))  # 3
        self.dec.append(nn.Dropout(0))
        self.dec.append(conv_block(dim, dec_nf[2] + enc_nf[0], dec_nf[3]))  # 4
        self.dec.append(nn.Dropout(0))
        self.dec.append(conv_block(dim, dec_nf[3], dec_nf[4]))  # 5
        self.dec.append(conv_block(dim, dec_nf[4] + input_ch, dec_nf[5], 1)) # 6
        

        self.vm2_conv = conv_block(dim, dec_nf[5], dec_nf[6]) 
 
        self.upsample = nn.Upsample(scale_factor=2, mode='nearest')

    def forward(self, x):
        """
        Pass input x through the UNet forward once
            :param x: concatenated fixed and moving image
        """
        # Get encoder activations
        x_enc = [x]
        y = x
        for i in range(len(self.enc)):
            layer = self.enc[i]
            y = layer(y)
            # logging.info(y.size())
            # import ipdb; ipdb.set_trace()
            if i%2==0:
                x_enc.append(y)
            # logging.info(layer)

        # Three conv + upsample + concatenate series
        # y = x_enc[-1]
        # import ipdb; ipdb.set_trace()
        # y=self.drop(y)
        concat_cnt = 0
        for i in range(len(self.dec)-1):
            layer = self.dec[i]
            y = layer(y)
            concat = (i%2==0) and (concat_cnt<3)
            if concat:
                y = self.upsample(y)
                # logging.info(i, y.size(), x_enc[-(i+2)].size())
                y = torch.cat([y, x_enc[-(concat_cnt+2)]], dim=1) #(-2,-3,-4)
                concat_cnt += 1
            # Two convs at full_size/2 res
            # y = layer(y)
            # y = self.dec[8](y)
            # y = self.dec[9](y)

        # Upsample to full res, concatenate and conv
        y = self.upsample(y)
        y = torch.cat([y, x_enc[0]], dim=1)
        # y = self.dec[-2](y)
        y = self.dec[-1](y)
        # Extra conv for vm2
        y = self.vm2_conv(y)

        return y

class RegNet(nn.Module):
    def __init__(self, size, dim=3, winsize=7,
                enc_nf=[16, 32, 32, 32], dec_nf= [32, 32, 32, 32, 32, 16, 16], n_class=29):
        super(RegNet, self).__init__()
        self.unet = unet_core(dim=dim, enc_nf = enc_nf, dec_nf = dec_nf)
        if(dim == 3):
            conv_fn = nn.Conv3d
        else:
            conv_fn = nn.Conv2d
        self.conv = conv_fn(dec_nf[-1], dim, kernel_size = 3, stride = 1, padding = 1)
        self.spatial_transformer_network = SpatialTransformer(size)
        self.winsize = winsize
        self.n_class = n_class
        #feat
        # self.feat_conv= conv_fn(dec_nf[-1]+num_classes, num_classes, kernel_size = 3, stride = 1, padding = 1)
        # self.softmax = nn.Softmax(dim=1)
        # self.drop = nn.Dropout(droprate)

    def eval_dice(self, fixed_label, moving_label, flow, fix_nopad=None):
        warped_seg= self.spatial_transformer_network(moving_label, flow)
        #
        if fix_nopad is not None:
            fixed_label = fix_nopad*fixed_label
            moving_label = fix_nopad*moving_label
        warplabel = torch.max(warped_seg.detach(),dim=1)[1]
        warpseg = torch.nn.functional.one_hot(warplabel.long(), num_classes=self.n_class).float().permute(0,4,1,2,3)
        dice = dice_onehot(warpseg[:,1:,:,:,:].detach(), fixed_label[:,1:,:,:,:].detach())#disregard background
        return dice

    def forward(self, fix, moving, fix_label, moving_label, fix_nopad=None, rtloss=True, eval=True):
        x = torch.cat([moving,fix], dim = 1)
        unet_out = self.unet(x)
        flow = self.conv(unet_out)

        if rtloss:
            warp = self.spatial_transformer_network(moving, flow)
            if fix_nopad is not None:
                fix = fix*fix_nopad
                warp = warp*fix_nopad
            sim_loss, sim_mask = ncc_loss(warp,fix, reduce_mean=False, winsize=self.winsize) #[0,1]
            grad_loss = gradient_loss(flow, keepdim=False).mean()
            if fix_nopad is not None:
                mask = fix_nopad.bool()
                sim_mask = sim_mask*mask
            sloss = sim_loss[sim_mask].mean()
            
            if eval:
                dice = self.eval_dice(fix_label, moving_label, flow)
                return sloss, grad_loss, dice
            else:
                return sloss, grad_loss
        else:
            if eval:
                dice = self.eval_dice(fix_label, moving_label, flow)
                return dice
            else:
                return flow


class RegUncertNet(RegNet):
    def __init__(self, size, dim=3, winsize=7,
                enc_nf=[16, 32, 32, 32], dec_nf= [32, 32, 32, 32, 32, 16, 16], n_class=29, grad_weight=0):
        super(RegNet, self).__init__()
        self.unet = unet_core(dim=dim, enc_nf = enc_nf, dec_nf = dec_nf)
        if(dim == 3):
            conv_fn = nn.Conv3d
        else:
            conv_fn = nn.Conv2d
        self.conv = conv_fn(dec_nf[-1], dim, kernel_size = 3, stride = 1, padding = 1)
        self.spatial_transformer_network = SpatialTransformer(size)
        self.winsize = winsize
        self.n_class = n_class
        #var
        self.conv_var = conv_fn(16, 32, kernel_size = 3, stride = 1, padding = 1)
        self.conv_var_2 = conv_fn(32, n_class, kernel_size = 3, stride = 1, padding = 1)
        self.softplus = nn.Softplus()
        self.grad_weight = grad_weight
        #feat
        # self.feat = feat
        # self.feat_conv= conv_fn(dec_nf[-1]+num_classes, num_classes, kernel_size = 3, stride = 1, padding = 1)
        # self.softmax = nn.Softmax(dim=1)
        # self.drop = nn.Dropout(droprate)
    
    def forward(self, fix, moving, fix_label, moving_label, gtflow=None, rtloss=True, eval=True):
        x = torch.cat([moving,fix], dim = 1)
        unet_out = self.unet(x)
        flow = self.conv(unet_out)
        out = self.conv_var(unet_out)
        out = self.conv_var_2(out)
        flow_var = self.softplus(out)

        warp = self.spatial_transformer_network(moving, flow)
        #
        if rtloss:
            #var
            
            dis = torch.nn.L1Loss(reduction='none')(flow, gtflow).mean(dim=1, keepdim=True)
            loss = (dis/flow_var+ flow_var.log()).mean()
            if self.grad_weight>0:
                loss += gradient_loss(flow, keepdim=False).mean()
            # sim_loss, sim_mask = ncc_loss(warp,fix, reduce_mean=False, winsize=self.winsize) #[0,1]
            # grad_loss = gradient_loss(flow, keepdim=False).mean()
            # sloss = sim_loss[sim_mask].mean()
            if eval:
                dice = self.eval_dice(fix_label, moving_label, flow)
                return loss, dice
            else:
                return loss
        else:
            if eval:
                dice = self.eval_dice(fix_label, moving_label, flow)
                return dice
            else:
                warped_seg= self.spatial_transformer_network(moving_label, flow)
                warplabel = torch.max(warped_seg.detach(),dim=1)[1]
                warpseg = torch.nn.functional.one_hot(warplabel.long(), num_classes=self.n_class).float().permute(0,4,1,2,3)
                return warpseg, flow, flow_var