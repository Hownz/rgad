import torch
from typing import Type, Any, Callable, Union, List, Optional
import torch.nn as nn
from torch import Tensor
import torch.nn.functional as F
from torchvision.models import resnet18
import torch.utils.model_zoo as model_zoo
from torch.autograd import Variable
import numpy as np
from .modules import *
import matplotlib.pyplot as plt

model_urls = {
    'resnet18': 'https://download.pytorch.org/models/resnet18-5c106cde.pth',
    'resnet34': 'https://download.pytorch.org/models/resnet34-333f7ec4.pth',
    'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
    'resnet101': 'https://download.pytorch.org/models/resnet101-5d3b4d8f.pth',
    'resnet152': 'https://download.pytorch.org/models/resnet152-b121ed2d.pth',
}

# N 参数
N_PARAMS = {'affine': 6, # 仿射变换矩阵 2*3
            'translation': 2, # 平移
            'rotation': 1, # 旋转
            'scale': 2, # 缩放
            'shear': 2, # 剪切
            'rotation_scale': 3, # 旋转+缩放
            'translation_scale': 4, # 平移+缩放
            'rotation_translation': 3, # 旋转+平移 
            'rotation_translation_scale': 5} # 旋转+平移+缩放

def conv1x1(in_planes: int, out_planes: int, stride: int = 1) -> nn.Conv2d:
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, 
                     kernel_size=1, stride=stride, 
                     bias=False)

def conv3x3(in_planes: int, out_planes: int, stride: int = 1, groups: int = 1, dilation: int = 1) -> nn.Conv2d:
    """3x3 convolution with padding"""
    return nn.Conv2d(
        in_planes, out_planes,
        kernel_size=3, stride=stride, # 1
        padding=dilation, dilation=dilation, # 1 padding 
        groups=groups, # 1
        bias=False, # False
    ) # (N-3+1*2)*1+1 = N

class STNModule(nn.Module):
    def __init__(self, args, in_num, feat_size):
        super(STNModule, self).__init__()
        # 针对于变换矩阵
        if args.mode == 'train':
            self.model_mode = 'train'
        else:
            self.model_mode = 'test'
        self.feat_size = feat_size # 输入 96=>96/2/2 = 24
        self.stn_mode = args.stn_mode
        self.stn_n_params = N_PARAMS[self.stn_mode]
        self.fn = nn.Sequential(
            # SPPF(128, self.feat_out), # shape 不变 （自己添加的）
            conv3x3(in_planes=in_num, out_planes=64), # 128 96
            nn.BatchNorm2d(64),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1), # 128-3+2 / 2 + 1 = 64  48 
            conv3x3(in_planes=64, out_planes=16), # 64
            nn.BatchNorm2d(16),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1), # 32   16   8   24
        )
        self.fc = nn.Sequential(
            # (6, 16, 24, 24)
            nn.Linear(in_features=16 * self.feat_size * self.feat_size, out_features=1024),
            nn.ReLU(True),
            nn.Linear(in_features=1024, out_features=self.stn_n_params),
        )
        self.fc[2].weight.data.fill_(0)
        self.fc[2].weight.data.zero_()
        if self.stn_mode == 'affine':
            self.fc[2].bias.data.copy_(torch.tensor([1, 0, 0, 0, 1, 0], dtype=torch.float))
        elif self.stn_mode in ['translation', 'shear']:
            self.fc[2].bias.data.copy_(torch.tensor([0, 0], dtype=torch.float))
        elif self.stn_mode == 'scale':
            self.fc[2].bias.data.copy_(torch.tensor([1, 1], dtype=torch.float))
        elif self.stn_mode == 'rotation':
            self.fc[2].bias.data.copy_(torch.tensor([0], dtype=torch.float))
        elif self.stn_mode == 'rotation_scale':
            self.fc[2].bias.data.copy_(torch.tensor([0, 1, 1], dtype=torch.float))
        elif self.stn_mode == 'translation_scale':
            self.fc[2].bias.data.copy_(torch.tensor([0, 0, 1, 1], dtype=torch.float))
        elif self.stn_mode == 'rotation_translation':
            self.fc[2].bias.data.copy_(torch.tensor([0, 0, 0], dtype=torch.float))
        elif self.stn_mode == 'rotation_translation_scale':
            self.fc[2].bias.data.copy_(torch.tensor([0, 0, 0, 1, 1], dtype=torch.float))
        

    def forward(self, x):
        mode = self.stn_mode
        batch_size = x.size(0)
        fix_x = self.fn(x) 

        theta = self.fc(fix_x.reshape(batch_size, -1)) # 3个参数=旋转+缩放

        if mode == 'affine':
            # affine 是 什么模式：仿射变换矩阵
            theta1 = theta.view(batch_size, 2, 3)
        else:
            if self.model_mode == 'train':
                theta1 = Variable(torch.zeros([batch_size, 2, 3], 
                                            dtype=torch.float32, 
                                            device=x.get_device()),
                                requires_grad=True) # 2*3的张量
            else:
                theta1 = Variable(torch.zeros([batch_size, 2, 3], 
                                            dtype=torch.float32, 
                                            device='cpu'),
                                requires_grad=True) # 2*3的张量
            theta1 = theta1 + 0
            theta1[:, 0, 0] = 1.0
            theta1[:, 1, 1] = 1.0
            if mode == 'translation':
                theta1[:, 0, 2] = theta[:, 0]
                theta1[:, 1, 2] = theta[:, 1]
            elif mode == 'rotation':
                angle = theta[:, 0]
                theta1[:, 0, 0] = torch.cos(angle)
                theta1[:, 0, 1] = -torch.sin(angle)
                theta1[:, 1, 0] = torch.sin(angle)
                theta1[:, 1, 1] = torch.cos(angle)
            elif mode == 'scale':
                theta1[:, 0, 0] = theta[:, 0]
                theta1[:, 1, 1] = theta[:, 1]
            elif mode == 'shear':
                theta1[:, 0, 1] = theta[:, 0]
                theta1[:, 1, 0] = theta[:, 1]
            elif mode == 'rotation_scale':
                # 旋转+缩放 = 3个参数
                # theta = [angle, scale_x, scale_y]
                angle = theta[:, 0]
                theta1[:, 0, 0] = torch.cos(angle) * theta[:, 1]
                theta1[:, 0, 1] = -torch.sin(angle)
                theta1[:, 1, 0] = torch.sin(angle)
                theta1[:, 1, 1] = torch.cos(angle) * theta[:, 2]
            elif mode == 'translation_scale':
                theta1[:, 0, 2] = theta[:, 0]
                theta1[:, 1, 2] = theta[:, 1]
                theta1[:, 0, 0] = theta[:, 2]
                theta1[:, 1, 1] = theta[:, 3]
            elif mode == 'rotation_translation':
                angle = theta[:, 0]
                theta1[:, 0, 0] = torch.cos(angle)
                theta1[:, 0, 1] = -torch.sin(angle)
                theta1[:, 1, 0] = torch.sin(angle)
                theta1[:, 1, 1] = torch.cos(angle)
                theta1[:, 0, 2] = theta[:, 1]
                theta1[:, 1, 2] = theta[:, 2]
            elif mode == 'rotation_translation_scale':
                angle = theta[:, 0]
                theta1[:, 0, 0] = torch.cos(angle) * theta[:, 3]
                theta1[:, 0, 1] = -torch.sin(angle)
                theta1[:, 1, 0] = torch.sin(angle)
                theta1[:, 1, 1] = torch.cos(angle) * theta[:, 4]
                theta1[:, 0, 2] = theta[:, 1]
                theta1[:, 1, 2] = theta[:, 2]

        # theta1 = theta1.view(batch_size, 2, 3):变换矩阵
        grid = F.affine_grid(theta1, torch.Size(x.shape)) # 旋转和缩放作用在x上
        # theta: 一个 N*2*3的张量，N是batch size。
        # size： 是得到的网格的尺度，也就是希望仿射变换之后得到的图像大小
        img_transform = F.grid_sample(x, grid, padding_mode="reflection") # 插值在x上

        return img_transform, theta1


def stn_net(args, pretrained=True):
    """Constructs a ResNet-18 model.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    from resnet import WideResNet_STN   
    model = WideResNet_STN(args, pretrained=True)
    if pretrained:
        model.load_state_dict(model_zoo.load_url(model_urls['resnet18']), strict=False)
    return model


class BasicBlock(nn.Module):
    expansion: int = 1

    def __init__(
            self,
            inplanes: int,
            planes: int,
            stride: int = 1,
            downsample: Optional[nn.Module] = None,
            groups: int = 1,
            base_width: int = 64,
            dilation: int = 1,
            norm_layer: Optional[Callable[..., nn.Module]] = None,
    ) -> None:
        super().__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        if groups != 1 or base_width != 64:
            raise ValueError("BasicBlock only supports groups=1 and base_width=64")
        if dilation > 1:
            raise NotImplementedError("Dilation > 1 not supported in BasicBlock")
        # Both self.conv1 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = norm_layer(planes)
        self.relu = nn.ReLU(inplace=True)

        self.conv2 = conv3x3(planes, planes)
        self.bn2 = norm_layer(planes)
        
        self.downsample = downsample
        
        self.stride = stride

    def forward(self, x: Tensor) -> Tensor:
        # 输入 x: torch.Size([32, 64, 56, 56])
        identity = x 

        out = self.conv1(x) # torch.Size([32, 64, 56, 56])
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out) # torch.Size([32, 64, 56, 56])
        out = self.bn2(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out

class ResNet(nn.Module):

    def __init__(self, args, block, layers):
        super(ResNet, self).__init__()
        self.inplanes = 64
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        
        # block = BasicBlock
        # layer = [2,2,2,2]
        self.layer1 = self._make_layer(block, 64, layers[0], stride=1) # layer[0] = 2
        self.stn1 = STNModule(64, 1, args)

        self.layer2 = self._make_layer(block, 128, layers[1], stride=2) # layer[1] = 2
        self.stn2 = STNModule(128, 2, args)

        self.layer3 = self._make_layer(block, 256, layers[2], stride=2) # layer[2] = 2
        self.stn3 = STNModule(256, 4, args) # 最后是8

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        # block.expansion = 1
        if stride != 1 or self.inplanes != planes * block.expansion: # 64 != 64 * 1
            downsample = nn.Sequential(
                conv1x1(self.inplanes, planes * block.expansion, stride),
                nn.BatchNorm2d(planes * block.expansion))

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample)) # 第一个block=BaiscBlock(64, 64, 1, downsample)
        self.inplanes = planes * block.expansion # 64
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def _fixstn(self, x, theta):
        # 含义是stn网络=>变换后的图像
        grid = F.affine_grid(theta, torch.Size(x.shape))
        img_transform = F.grid_sample(x, grid, padding_mode="reflection")

        return img_transform

    def forward(self, x: Tensor) -> Tensor:
        # 输入 x: torch.Size([32, 3, 224, 224])
        x = self.conv1(x) # torch.Size([32, 64, 112, 112]) 112 = (224-7+2*3)/2+1
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x) # torch.Size([32, 64, 56, 56]) 56 = (112-3)/2+1
        # torch.Size([32, 64, 56, 56])

        x = self.layer1(x) # torch.Size([32, 64, 56, 56]) 56 = (56-3)/1+1   128
        x, theta1 = self.stn1(x) # x是变换后的图像，theta1是变换矩阵
        tmp = np.tile(np.array([0, 0, 1]), (x.shape[0], 1, 1)).astype(np.float32)
        fixthea1 = torch.from_numpy(np.linalg.inv(np.concatenate((theta1.detach().cpu().numpy(), tmp), axis=1))[:,:-1,:]).cpu() # cuda(1)
        self.stn1_output = self._fixstn(x.detach().cpu(), fixthea1) # 可视化的时候用
        # after layer1 shape:  torch.Size([32, 64, 56, 56])

        x = self.layer2(x) # torch.Size([32, 128, 28, 28]) 28 = (56-3)/2+1   64
        x, theta2 = self.stn2(x)
        tmp = np.tile(np.array([0, 0, 1]), (x.shape[0], 1, 1)).astype(np.float32)
        fixthea2 = torch.from_numpy(np.linalg.inv(np.concatenate((theta2.detach().cpu().numpy(), tmp), axis=1))[:,:-1,:]).cpu() # cuda(1)
        self.stn2_output = self._fixstn(self._fixstn(x.detach().cpu(), fixthea2), fixthea1) # 恢复
        # after layer2 shape:  torch.Size([32, 128, 28, 28])

        x = self.layer3(x) # torch.Size([32, 256, 14, 14]) 14 = (28-3)/2+1   32
        out, theta3 = self.stn3(x)
        tmp = np.tile(np.array([0, 0, 1]), (x.shape[0], 1, 1)).astype(np.float32)
        fixthea3 = torch.from_numpy(np.linalg.inv(np.concatenate((theta3.detach().cpu().numpy(), tmp), axis=1))[:,:-1,:]).cpu() # cuda(1)
        self.stn3_output = self._fixstn(self._fixstn(self._fixstn(out.detach().cpu(), fixthea3), fixthea2), fixthea1) # 恢复
        # after layer3 shape:  torch.Size([32, 256, 14, 14])

        return out
