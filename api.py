
import subprocess, os, sys


google_drive = False
save_models_to_google_drive = False

root_path = os.getcwd()

import os
def createPath(filepath):
    os.makedirs(filepath, exist_ok=True)

initDirPath = f'{root_path}/init_images'
createPath(initDirPath)
outDirPath = f'{root_path}/images_out'
createPath(outDirPath)

model_path = f'{root_path}/models'
# createPath(model_path)

import pathlib, shutil, os, sys
os.environ['KMP_DUPLICATE_LIB_OK']='TRUE'

PROJECT_DIR = os.path.abspath(os.getcwd())
USE_ADABINS = True

sys.path.append(os.path.join(root_path, 'CLIP'))
sys.path.append(os.path.join(root_path, 'guided-diffusion'))
sys.path.append(os.path.join(root_path, 'ResizeRight'))
sys.path.append(os.path.join(root_path, 'pytorch3d-lite'))
sys.path.append(os.path.join(root_path, 'MiDaS'))
sys.path.append(root_path)
sys.path.append(os.path.join(root_path, 'AdaBins'))

from CLIP import clip
from guided_diffusion.script_util import create_model_and_diffusion
from resize_right import resize
import py3d_tools
from midas.dpt_depth import DPTDepthModel
import disco_xform_utils as dxf


import torch
from dataclasses import dataclass
from functools import partial
import cv2
import pandas as pd
import gc
import io
import math
import timm
import lpips
from PIL import Image, ImageOps
import requests
from glob import glob
import json
from types import SimpleNamespace
from torch import nn
from torch.nn import functional as F
import torchvision.transforms as T
import torchvision.transforms.functional as TF
from CLIP import clip
from resize_right import resize
from guided_diffusion.script_util import create_model_and_diffusion, model_and_diffusion_defaults
from datetime import datetime
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter
import random
import hashlib
from functools import partial
os.chdir(f'{PROJECT_DIR}')
from numpy import asarray
from einops import rearrange, repeat
import torch, torchvision
import time
from omegaconf import OmegaConf
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

from infer import InferenceHelper

MAX_ADABINS_AREA = 500000

import torch
DEVICE = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
print('Using device:', DEVICE)
device = DEVICE # At least one of the modules expects this name..

if torch.cuda.get_device_capability(DEVICE) == (8,0): ## A100 fix thanks to Emad
  print('Disabling CUDNN for A100 gpu', file=sys.stderr)
  torch.backends.cudnn.enabled = False

#zippy noise_injector stuff csa
from PIL import Image
import numpy as np
from matplotlib import pyplot as plt
import argparse
import imutils
import cv2
from skimage import exposure

#@title ### 1.4 Define Midas functions

from midas.dpt_depth import DPTDepthModel
from midas.midas_net import MidasNet
from midas.midas_net_custom import MidasNet_small
from midas.transforms import Resize, NormalizeImage, PrepareForNet

# Initialize MiDaS depth model.
# It remains resident in VRAM and likely takes around 2GB VRAM.
# You could instead initialize it for each frame (and free it after each frame) to save VRAM.. but initializing it is slow.
default_models = {
    "midas_v21_small": f"{model_path}/midas_v21_small-70d6b9c8.pt",
    "midas_v21": f"{model_path}/midas_v21-f6b98070.pt",
    "dpt_large": f"{model_path}/dpt_large-midas-2f21e586.pt",
    "dpt_hybrid": f"{model_path}/dpt_hybrid-midas-501f0c75.pt",
    "dpt_hybrid_nyu": f"{model_path}/dpt_hybrid_nyu-2ce69ec7.pt",}


def init_midas_depth_model(midas_model_type="dpt_large", optimize=True):
    midas_model = None
    net_w = None
    net_h = None
    resize_mode = None
    normalization = None

    print(f"Initializing MiDaS '{midas_model_type}' depth model...")
    # load network
    midas_model_path = default_models[midas_model_type]

    if midas_model_type == "dpt_large": # DPT-Large
        midas_model = DPTDepthModel(
            path=midas_model_path,
            backbone="vitl16_384",
            non_negative=True,
        )
        net_w, net_h = 384, 384
        resize_mode = "minimal"
        normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
    elif midas_model_type == "dpt_hybrid": #DPT-Hybrid
        midas_model = DPTDepthModel(
            path=midas_model_path,
            backbone="vitb_rn50_384",
            non_negative=True,
        )
        net_w, net_h = 384, 384
        resize_mode="minimal"
        normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
    elif midas_model_type == "dpt_hybrid_nyu": #DPT-Hybrid-NYU
        midas_model = DPTDepthModel(
            path=midas_model_path,
            backbone="vitb_rn50_384",
            non_negative=True,
        )
        net_w, net_h = 384, 384
        resize_mode="minimal"
        normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
    elif midas_model_type == "midas_v21":
        midas_model = MidasNet(midas_model_path, non_negative=True)
        net_w, net_h = 384, 384
        resize_mode="upper_bound"
        normalization = NormalizeImage(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
        )
    elif midas_model_type == "midas_v21_small":
        midas_model = MidasNet_small(midas_model_path, features=64, backbone="efficientnet_lite3", exportable=True, non_negative=True, blocks={'expand': True})
        net_w, net_h = 256, 256
        resize_mode="upper_bound"
        normalization = NormalizeImage(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
        )
    else:
        print(f"midas_model_type '{midas_model_type}' not implemented")
        assert False

    midas_transform = T.Compose(
        [
            Resize(
                net_w,
                net_h,
                resize_target=None,
                keep_aspect_ratio=True,
                ensure_multiple_of=32,
                resize_method=resize_mode,
                image_interpolation_method=cv2.INTER_CUBIC,
            ),
            normalization,
            PrepareForNet(),
        ]
    )

    midas_model.eval()
    
    if optimize==True:
        if DEVICE == torch.device("cuda"):
            midas_model = midas_model.to(memory_format=torch.channels_last)  
            midas_model = midas_model.half()

    midas_model.to(DEVICE)

    print(f"MiDaS '{midas_model_type}' depth model initialized.")
    return midas_model, midas_transform, net_w, net_h, resize_mode, normalization

import py3d_tools as p3dT
import disco_xform_utils as dxf

def interp(t):
    return 3 * t**2 - 2 * t ** 3

def perlin(width, height, scale=10, device=None):
    gx, gy = torch.randn(2, width + 1, height + 1, 1, 1, device=device)
    xs = torch.linspace(0, 1, scale + 1)[:-1, None].to(device)
    ys = torch.linspace(0, 1, scale + 1)[None, :-1].to(device)
    wx = 1 - interp(xs)
    wy = 1 - interp(ys)
    dots = 0
    dots += wx * wy * (gx[:-1, :-1] * xs + gy[:-1, :-1] * ys)
    dots += (1 - wx) * wy * (-gx[1:, :-1] * (1 - xs) + gy[1:, :-1] * ys)
    dots += wx * (1 - wy) * (gx[:-1, 1:] * xs - gy[:-1, 1:] * (1 - ys))
    dots += (1 - wx) * (1 - wy) * (-gx[1:, 1:] * (1 - xs) - gy[1:, 1:] * (1 - ys))
    return dots.permute(0, 2, 1, 3).contiguous().view(width * scale, height * scale)

def perlin_ms(octaves, width, height, grayscale, device=device):
    out_array = [0.5] if grayscale else [0.5, 0.5, 0.5]
    # out_array = [0.0] if grayscale else [0.0, 0.0, 0.0]
    for i in range(1 if grayscale else 3):
        scale = 2 ** len(octaves)
        oct_width = width
        oct_height = height
        for oct in octaves:
            p = perlin(oct_width, oct_height, scale, device)
            out_array[i] += p * oct
            scale //= 2
            oct_width *= 2
            oct_height *= 2
    return torch.cat(out_array)

def create_perlin_noise(octaves=[1, 1, 1, 1], width=2, height=2, grayscale=True):
    out = perlin_ms(octaves, width, height, grayscale)
    if grayscale:
        out = TF.resize(size=(side_y, side_x), img=out.unsqueeze(0))
        out = TF.to_pil_image(out.clamp(0, 1)).convert('RGB')
    else:
        out = out.reshape(-1, 3, out.shape[0]//3, out.shape[1])
        out = TF.resize(size=(side_y, side_x), img=out)
        out = TF.to_pil_image(out.clamp(0, 1).squeeze())

    out = ImageOps.autocontrast(out)
    return out

def regen_perlin():
    if perlin_mode == 'color':
        init = create_perlin_noise([1.5**-i*0.5 for i in range(12)], 1, 1, False)
        init2 = create_perlin_noise([1.5**-i*0.5 for i in range(8)], 4, 4, False)
    elif perlin_mode == 'gray':
        init = create_perlin_noise([1.5**-i*0.5 for i in range(12)], 1, 1, True)
        init2 = create_perlin_noise([1.5**-i*0.5 for i in range(8)], 4, 4, True)
    else:
        init = create_perlin_noise([1.5**-i*0.5 for i in range(12)], 1, 1, False)
        init2 = create_perlin_noise([1.5**-i*0.5 for i in range(8)], 4, 4, True)

    init = TF.to_tensor(init).add(TF.to_tensor(init2)).div(2).to(device).unsqueeze(0).mul(2).sub(1)
    del init2
    return init.expand(batch_size, -1, -1, -1)

def fetch(url_or_path):
    if str(url_or_path).startswith('http://') or str(url_or_path).startswith('https://'):
        r = requests.get(url_or_path)
        r.raise_for_status()
        fd = io.BytesIO()
        fd.write(r.content)
        fd.seek(0)
        return fd
    return open(url_or_path, 'rb')

def read_image_workaround(path):
    """OpenCV reads images as BGR, Pillow saves them as RGB. Work around
    this incompatibility to avoid colour inversions."""
    im_tmp = cv2.imread(path)
    return cv2.cvtColor(im_tmp, cv2.COLOR_BGR2RGB)

def parse_prompt(prompt):
    if prompt.startswith('http://') or prompt.startswith('https://'):
        vals = prompt.rsplit(':', 2)
        vals = [vals[0] + ':' + vals[1], *vals[2:]]
    else:
        vals = prompt.rsplit(':', 1)
    vals = vals + ['', '1'][len(vals):]
    return vals[0], float(vals[1])

def sinc(x):
    return torch.where(x != 0, torch.sin(math.pi * x) / (math.pi * x), x.new_ones([]))

def lanczos(x, a):
    cond = torch.logical_and(-a < x, x < a)
    out = torch.where(cond, sinc(x) * sinc(x/a), x.new_zeros([]))
    return out / out.sum()

def ramp(ratio, width):
    n = math.ceil(width / ratio + 1)
    out = torch.empty([n])
    cur = 0
    for i in range(out.shape[0]):
        out[i] = cur
        cur += ratio
    return torch.cat([-out[1:].flip([0]), out])[1:-1]

def resample(input, size, align_corners=True):
    n, c, h, w = input.shape
    dh, dw = size

    input = input.reshape([n * c, 1, h, w])

    if dh < h:
        kernel_h = lanczos(ramp(dh / h, 2), 2).to(input.device, input.dtype)
        pad_h = (kernel_h.shape[0] - 1) // 2
        input = F.pad(input, (0, 0, pad_h, pad_h), 'reflect')
        input = F.conv2d(input, kernel_h[None, None, :, None])

    if dw < w:
        kernel_w = lanczos(ramp(dw / w, 2), 2).to(input.device, input.dtype)
        pad_w = (kernel_w.shape[0] - 1) // 2
        input = F.pad(input, (pad_w, pad_w, 0, 0), 'reflect')
        input = F.conv2d(input, kernel_w[None, None, None, :])

    input = input.reshape([n, c, h, w])
    return F.interpolate(input, size, mode='bicubic', align_corners=align_corners)

class MakeCutouts(nn.Module):
    def __init__(self, cut_size, cutn, skip_augs=False):
        super().__init__()
        self.cut_size = cut_size
        self.cutn = cutn
        self.skip_augs = skip_augs
        self.augs = T.Compose([
            T.RandomHorizontalFlip(p=0.5),
            T.Lambda(lambda x: x + torch.randn_like(x) * 0.01),
            T.RandomAffine(degrees=15, translate=(0.1, 0.1)),
            T.Lambda(lambda x: x + torch.randn_like(x) * 0.01),
            T.RandomPerspective(distortion_scale=0.4, p=0.7),
            T.Lambda(lambda x: x + torch.randn_like(x) * 0.01),
            T.RandomGrayscale(p=0.15),
            T.Lambda(lambda x: x + torch.randn_like(x) * 0.01),
            # T.ColorJitter(brightness=0.1, contrast=0.1, saturation=0.1, hue=0.1),
        ])

    def forward(self, input):
        input = T.Pad(input.shape[2]//4, fill=0)(input)
        sideY, sideX = input.shape[2:4]
        max_size = min(sideX, sideY)

        cutouts = []
        for ch in range(self.cutn):
            if ch > self.cutn - self.cutn//4:
                cutout = input.clone()
            else:
                size = int(max_size * torch.zeros(1,).normal_(mean=.8, std=.3).clip(float(self.cut_size/max_size), 1.))
                offsetx = torch.randint(0, abs(sideX - size + 1), ())
                offsety = torch.randint(0, abs(sideY - size + 1), ())
                cutout = input[:, :, offsety:offsety + size, offsetx:offsetx + size]

            if not self.skip_augs:
                cutout = self.augs(cutout)
            cutouts.append(resample(cutout, (self.cut_size, self.cut_size)))
            del cutout

        cutouts = torch.cat(cutouts, dim=0)
        return cutouts

cutout_debug = False
padargs = {}

class MakeCutoutsDango(nn.Module):
    def __init__(self, cut_size,
                 Overview=4, 
                 InnerCrop = 0, IC_Size_Pow=0.5, IC_Grey_P = 0.2
                 ):
        super().__init__()
        self.cut_size = cut_size
        self.Overview = Overview
        self.InnerCrop = InnerCrop
        self.IC_Size_Pow = IC_Size_Pow
        self.IC_Grey_P = IC_Grey_P
          

    def forward(self, input):
        cutouts = []
        gray = T.Grayscale(3)
        sideY, sideX = input.shape[2:4]
        max_size = min(sideX, sideY)
        min_size = min(sideX, sideY, self.cut_size)
        l_size = max(sideX, sideY)
        output_shape = [1,3,self.cut_size,self.cut_size] 
        output_shape_2 = [1,3,self.cut_size+2,self.cut_size+2]
        pad_input = F.pad(input,((sideY-max_size)//2,(sideY-max_size)//2,(sideX-max_size)//2,(sideX-max_size)//2), **padargs)
        cutout = resize(pad_input, out_shape=output_shape)

        if self.Overview>0:
            if self.Overview<=4:
                if self.Overview>=1:
                    cutouts.append(cutout)
                if self.Overview>=2:
                    cutouts.append(gray(cutout))
                if self.Overview>=3:
                    cutouts.append(TF.hflip(cutout))
                if self.Overview==4:
                    cutouts.append(gray(TF.hflip(cutout)))
            else:
                cutout = resize(pad_input, out_shape=output_shape)
                for _ in range(self.Overview):
                    cutouts.append(cutout)

            if cutout_debug:
                TF.to_pil_image(cutouts[0].clamp(0, 1).squeeze(0)).save("cutout_overview0.jpg",quality=99)

                              
        if self.InnerCrop >0:
            for i in range(self.InnerCrop):
                size = int(torch.rand([])**self.IC_Size_Pow * (max_size - min_size) + min_size)
                offsetx = torch.randint(0, sideX - size + 1, ())
                offsety = torch.randint(0, sideY - size + 1, ())
                cutout = input[:, :, offsety:offsety + size, offsetx:offsetx + size]
                if i <= int(self.IC_Grey_P * self.InnerCrop):
                    cutout = gray(cutout)
                cutout = resize(cutout, out_shape=output_shape)
                cutouts.append(cutout)
        cutouts = torch.cat(cutouts)
        #if skip_augs is not True: cutouts=self.augs(cutouts)
        return cutouts

def spherical_dist_loss(x, y):
    x = F.normalize(x, dim=-1)
    y = F.normalize(y, dim=-1)
    return (x - y).norm(dim=-1).div(2).arcsin().pow(2).mul(2)     

def tv_loss(input):
    """L2 total variation loss, as in Mahendran et al."""
    input = F.pad(input, (0, 1, 0, 1), 'replicate')
    x_diff = input[..., :-1, 1:] - input[..., :-1, :-1]
    y_diff = input[..., 1:, :-1] - input[..., :-1, :-1]
    return (x_diff**2 + y_diff**2).mean([1, 2, 3])


def range_loss(input):
    return (input - input.clamp(-1, 1)).pow(2).mean([1, 2, 3])

stop_on_next_loop = False  # Make sure GPU memory doesn't get corrupted from cancelling the run mid-way through, allow a full frame to complete
TRANSLATION_SCALE = 1.0/200.0

def do_3d_step(img_filepath, frame_num, midas_model, midas_transform):
  if args.key_frames:
    translation_x = args.translation_x_series[frame_num]
    translation_y = args.translation_y_series[frame_num]
    translation_z = args.translation_z_series[frame_num]
    rotation_3d_x = args.rotation_3d_x_series[frame_num]
    rotation_3d_y = args.rotation_3d_y_series[frame_num]
    rotation_3d_z = args.rotation_3d_z_series[frame_num]
    print(
        f'translation_x: {translation_x}',
        f'translation_y: {translation_y}',
        f'translation_z: {translation_z}',
        f'rotation_3d_x: {rotation_3d_x}',
        f'rotation_3d_y: {rotation_3d_y}',
        f'rotation_3d_z: {rotation_3d_z}',
    )

  translate_xyz = [-translation_x*TRANSLATION_SCALE, translation_y*TRANSLATION_SCALE, -translation_z*TRANSLATION_SCALE]
  rotate_xyz_degrees = [rotation_3d_x, rotation_3d_y, rotation_3d_z]
  print('translation:',translate_xyz)
  print('rotation:',rotate_xyz_degrees)
  rotate_xyz = [math.radians(rotate_xyz_degrees[0]), math.radians(rotate_xyz_degrees[1]), math.radians(rotate_xyz_degrees[2])]
  rot_mat = p3dT.euler_angles_to_matrix(torch.tensor(rotate_xyz, device=device), "XYZ").unsqueeze(0)
  print("rot_mat: " + str(rot_mat))
  next_step_pil = dxf.transform_image_3d(img_filepath, midas_model, midas_transform, DEVICE,
                                          rot_mat, translate_xyz, args.near_plane, args.far_plane,
                                          args.fov, padding_mode=args.padding_mode,
                                          sampling_mode=args.sampling_mode, midas_weight=args.midas_weight)
  return next_step_pil


def soft_limit(x):
  #zippy soft limiter
  #value from -n to n, always return a value -1<x<1
  #soft_limiter_knee set in params = 0.97 # where does compression start?
  soft_sign = x/abs(x)
  soft_overage = ((abs(x)-soft_limiter_knee)+(abs(abs(x)-soft_limiter_knee)))/2
  soft_base = abs(x)-soft_overage
  soft_limited_x = soft_base + torch.tanh(soft_overage/(1-soft_limiter_knee))*(1-soft_limiter_knee)
  return soft_limited_x*soft_sign

def center_crop(img,crop_pct):
  #zippy color kit
  dimensions = img.shape 
  # height, width, number of channels in image
  height = img.shape[0]
  width = img.shape[1]
  channels = img.shape[2]
  h1=int((height/2)*(1-crop_pct))
  h2=int((height/2)*(1+crop_pct))
  w1=int((width/2)*(1-crop_pct))
  w2=int((width/2)*(1+crop_pct))
  crop_img = img[h1:h2, w1:w2]
  return crop_img

def get_edge_img(img):
  #zippy color kit
  canny_min = .66*img.mean()
  canny_max = 1.33*img.mean()  
  get_edge_img = cv2.Canny(img,canny_min,canny_max,True) 
  return get_edge_img

def get_edge_mix(img):
  #zippy color kit
  edges = get_edge_img(img)
  edge_mix = edges.mean()
  return edge_mix

def calc_contrast(img):
  #from https://stackoverflow.com/questions/58821130/how-to-calculate-the-contrast-of-an-image
  img_grey = cv2.cvtColor((img*255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
  contrast = img_grey.std()
  return contrast

def calc_brightness(img):
  #zippy color kit
  #from https://stackoverflow.com/questions/58821130/how-to-calculate-the-contrast-of-an-image
  img_grey = cv2.cvtColor((img*255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
  brightness = img_grey.mean()
  return brightness

def contrast_stretch(img):
  #zippy color kit
  #requires a cv2 Image (val 0-1) 
  #also https://stackoverflow.com/questions/64484423/cv2-lut-throws-an-error-when-performing-gamma-correction-assertion-failed
  xp = [0, 64, 128, 192, 255]
  fp = [0, 16, 128, 240, 255]
  x = np.arange(256)
  table = np.interp(x, xp, fp).astype('uint8')
  #img = cv2.LUT((255*img).astype(np.uint8), table)
  #img = (img.astype(np.float)) / 255
  img = cv2.LUT((img).astype(np.uint8), table)
  img = (img.astype(np.float))
  return img

def contrast_stretch_lite(img):
  #zippy color kit
  xp = [0, 64, 128, 192, 255]
  fp = [0, 32, 128, 224, 255]
  x = np.arange(256)
  table = np.interp(x, xp, fp).astype('uint8')
  #img = cv2.LUT((255*img).astype(np.uint8), table)
  #img = (img.astype(np.float)) / 255
  img = cv2.LUT((img).astype(np.uint8), table)
  img = (img.astype(np.float))
  return img

def add_noise(img,noise_mix,noise_injector_px_size,
              noise_injector_spice,noise_injector_mask_feather,
              noise_injector_sigma):
  # zippy noise injector csa
  # make edge map from edge image
  width = int(img.shape[1])
  height = int(img.shape[0])
  dim_hw = (height, width)
  dim_wh = (width, height)

  dim = (int(img.shape[1]), int(img.shape[0]))
  edge_img = get_edge_img(img)
  kernel = np.ones((7,7), np.uint8)
  kernel2 = np.ones((21,21), np.uint8)
  edge_img_dilation = cv2.dilate(edge_img, kernel, iterations=3)
  edge_img_dilation2 = cv2.dilate(edge_img, kernel2, iterations=3)
  blended_dilation = (edge_img_dilation/2 + edge_img_dilation2/2)
  blur_size = noise_injector_mask_feather*2+1
  
  blur_kernel = (blur_size,blur_size)
  #3 blurs, just for grins
  edge_blurred_img = cv2.GaussianBlur(blended_dilation, blur_kernel, 0)
  edge_blurred_img = cv2.GaussianBlur(edge_blurred_img, blur_kernel, 0)
  edge_blurred_img = cv2.GaussianBlur(edge_blurred_img, blur_kernel, 0)
  mask = (255-edge_blurred_img)
  mask = mask.reshape(*mask.shape, 1)/255

  #make gaussian mask
  #https://stackoverflow.com/questions/55261087/adding-gaussian-noise-to-image
  mean = 0
  #var = 100
  #sigma = var ** 0.5
  sigma = noise_injector_sigma
  #sigma = 64 #larger numbers = bigger blobs
  #noise image is expecting dim_hw
  gaussian = np.random.normal(mean, sigma, dim_hw) # dim must be (H,W) 
  gaus_blur_kernel = (15,15)
  blurred_gaussian = cv2.GaussianBlur(gaussian, gaus_blur_kernel, 0)
  gaussian_mask = blurred_gaussian.reshape(*blurred_gaussian.shape, 1)
  mask = np.clip(mask, 0, 1)
  gaussian_mask = np.clip(gaussian_mask, 0, 1)
  combo_mask = mask*gaussian_mask

  #now make a noise pattern
  noise_img =  np.random.normal(loc=0, scale=1, size=img.shape)
  noise_img *= 255
  #change pixel density...
  noise_px_size = noise_injector_px_size  
  noise_img = center_crop(noise_img,1/noise_px_size)
  #modify noise color mix to original img
  colored_noise = exposure.match_histograms(noise_img, img, multichannel=True)
  #blend in 'spice' 
  colored_noise = noise_img * noise_injector_spice + colored_noise * (1-noise_injector_spice)  
  
  #resize to orig.. using dim from above
  #cv2.resize is expecting dim_wh
  colored_noise = cv2.resize(colored_noise, dim_wh, interpolation = cv2.INTER_AREA) 
  noise_blur_size = noise_px_size * 2 + 1
  noise_blur_kernel = (noise_blur_size,noise_blur_size)    
  colored_noise = cv2.GaussianBlur(colored_noise, noise_blur_kernel, 0)
  colored_noise = cv2.GaussianBlur(colored_noise, noise_blur_kernel, 0) 
  
  masked_noise = colored_noise * combo_mask
  scaled_mask = combo_mask * noise_mix
  #blend in noise using mask
  final_img = colored_noise * scaled_mask + img * (1 - scaled_mask)

  #uncomment below to see elements of noise injector
  #cv2_imshow(edge_img)
  #cv2_imshow(blended_dilation)
  #cv2_imshow(mask*255)#mask is 0-1
  #cv2_imshow(gaussian_mask*255)
  #cv2_imshow(combo_mask*255)
  #cv2_imshow(noise_img)
  #cv2_imshow(colored_noise)
  #cv2_imshow(final_img)
  #print('corrected contrast is ',(calc_contrast(final_img)))
  #print(calc_contrast(final_img))
  return final_img

  def symmetry_transformation_fn(x):
    if args.use_horizontal_symmetry:
        [n, c, h, w] = x.size()
        x = torch.concat((x[:, :, :, :w//2], torch.flip(x[:, :, :, :w//2], [-1])), -1)
        print("horizontal symmetry applied")
    if args.use_vertical_symmetry:
        [n, c, h, w] = x.size()
        x = torch.concat((x[:, :, :h//2, :], torch.flip(x[:, :, :h//2, :], [-2])), -2)
        print("vertical symmetry applied")

def makeArgs(batchNum, seed, display_rate, n_batches, batch_size, start_frame,calc_frames_skip_steps , skip_step_ratio, display_histogram):
  args = {
    'batchNum': batchNum,
    'prompts_series':split_prompts(text_prompts) if text_prompts else None,
    'image_prompts_series':split_prompts(image_prompts) if image_prompts else None,
    'seed': seed,
    'display_rate':display_rate,
    'n_batches':n_batches if animation_mode == 'None' else 1,
    'batch_size':batch_size,
    'batch_name': batch_name,
    'steps': steps,
    'diffusion_sampling_mode': diffusion_sampling_mode,
    'width_height': width_height,
    'clip_guidance_scale': clip_guidance_scale,
    'tv_scale': tv_scale,
    'range_scale': range_scale,
    'sat_scale': sat_scale,
    'sat_scale_buffer': sat_scale_buffer,
    'cutn_batches': cutn_batches,
    'soft_limiter_on': soft_limiter_on,
    'soft_limiter_knee': soft_limiter_knee,
    'init_image': None,
    'init_scale': init_scale,
    'skip_steps': skip_steps,
    'skip_end_steps': skip_end_steps,
    'side_x': side_x,
    'side_y': side_y,
    'timestep_respacing': timestep_respacing,
    'diffusion_steps': diffusion_steps,
    'animation_mode': animation_mode,
    'video_init_path': video_init_path,
    'extract_nth_frame': extract_nth_frame,
    'video_init_seed_continuity': video_init_seed_continuity,
    'key_frames': key_frames,
    'max_frames': max_frames if animation_mode != "None" else 1,
    'interp_spline': interp_spline,
    'start_frame': start_frame,
    'angle': angle,
    'zoom': zoom,
    'translation_x': translation_x,
    'translation_y': translation_y,
    'translation_z': translation_z,
    'rotation_3d_x': rotation_3d_x,
    'rotation_3d_y': rotation_3d_y,
    'rotation_3d_z': rotation_3d_z,
    'midas_depth_model': midas_depth_model,
    'midas_weight': midas_weight,
    'near_plane': near_plane,
    'far_plane': far_plane,
    'fov': fov,
    'padding_mode': padding_mode,
    'sampling_mode': sampling_mode,
    'angle_series':angle_series,
    'zoom_series':zoom_series,
    'translation_x_series':translation_x_series,
    'translation_y_series':translation_y_series,
    'translation_z_series':translation_z_series,
    'rotation_3d_x_series':rotation_3d_x_series,
    'rotation_3d_y_series':rotation_3d_y_series,
    'rotation_3d_z_series':rotation_3d_z_series,
    'frames_scale': frames_scale,
    'calc_frames_skip_steps': calc_frames_skip_steps,
    'skip_step_ratio': skip_step_ratio,
    'frames_skip_end_steps': frames_skip_end_steps,
    'text_prompts': text_prompts,
    'image_prompts': image_prompts,
    'cut_overview': eval(cut_overview),
      'cut_innercut': eval(cut_innercut),
      'cut_ic_pow': eval(cut_ic_pow),
      'cut_icgray_p': eval(cut_icgray_p),
      'intermediate_saves': intermediate_saves,
      'intermediates_in_subfolder': intermediates_in_subfolder,
      'steps_per_checkpoint': steps_per_checkpoint,
      'perlin_init': perlin_init,
      'perlin_mode': perlin_mode,
      'set_seed': set_seed,
      'eta': eta,
      'clamp_grad': clamp_grad,
      'clamp_max': clamp_max,
      'skip_augs': skip_augs,
      'randomize_class': randomize_class,
      'clip_denoised': clip_denoised,
      'fuzzy_prompt': fuzzy_prompt,
      'rand_mag': rand_mag,
      'use_vertical_symmetry': use_vertical_symmetry,
      'use_horizontal_symmetry': use_horizontal_symmetry,
      'transformation_percent': transformation_percent,
      'display_histogram': display_histogram,
      'noise_injector': noise_injector,
      'noise_injector_threshold': noise_injector_threshold,
      'noise_injector_mix': noise_injector_mix,
      'noise_injector_px_size': noise_injector_px_size,
      'noise_injector_spice': noise_injector_spice,
      'noise_injector_mask_feather': noise_injector_mask_feather,
      'noise_injector_sigma': noise_injector_sigma,
      'copy_palette': copy_palette,
      'copy_palette_image': copy_palette_image,
  }

  args = SimpleNamespace(**args)
  return args

def do_run(_prompt):
  batchFolder = f'{outDirPath}/{batch_name + "_" + str(time.time())}'
  createPath(batchFolder)
  display_rate = 10 #@param{type: 'number'}
  n_batches =   1#@param{type: 'number'}
  display_histogram = False#@param{type: 'boolean'}

  #Update Model Settings
  timestep_respacing = f'ddim{steps}'
  diffusion_steps = (1000//steps)*steps if steps < 1000 else steps
  model_config.update({
      'timestep_respacing': timestep_respacing,
      'diffusion_steps': diffusion_steps,
  })
  
  batch_size = 1 
  
  def move_files(start_num, end_num, old_folder, new_folder):
      for i in range(start_num, end_num):
          old_file = old_folder + f'/{batch_name}({batchNum})_{i:04}.png'
          new_file = new_folder + f'/{batch_name}({batchNum})_{i:04}.png'
          os.rename(old_file, new_file)

  resume_run = False #@param{type: 'boolean'}
  run_to_resume = 'latest' #@param{type: 'string'}
  resume_from_frame = 'latest' #@param{type: 'string'}
  retain_overwritten_frames = False #@param{type: 'boolean'}
  if retain_overwritten_frames:
      retainFolder = f'{batchFolder}/retained'
      createPath(retainFolder)
  
  skip_step_ratio = int(frames_skip_steps.rstrip("%")) / 100
  calc_frames_skip_steps = math.floor(steps * skip_step_ratio)
  
  
  if steps <= calc_frames_skip_steps:
    sys.exit("ERROR: You can't skip more steps than your total steps")
  start_frame = 0
  batchNum = len(glob(batchFolder+"/*.png"))
  while os.path.isfile(f"{batchFolder}/{batch_name}({batchNum})_0.png"):
      batchNum += 1

  print(f'Starting Run: {batch_name}({batchNum}) at frame {start_frame}')

  if set_seed == 'random_seed':
      random.seed()
      seed = random.randint(0, 2**32)
      # print(f'Using seed: {seed}')
  else:
      seed = int(set_seed)

  args = makeArgs(batchNum, seed, display_rate, n_batches, batch_size, start_frame,calc_frames_skip_steps , skip_step_ratio, display_histogram)
  seed = args.seed
  print(range(args.start_frame, args.max_frames))

  if (args.animation_mode == "3D") and (args.midas_weight > 0.0):
      midas_model, midas_transform, midas_net_w, midas_net_h, midas_resize_mode, midas_normalization = init_midas_depth_model(args.midas_depth_model)
  for frame_num in range(args.start_frame, args.max_frames):
      if stop_on_next_loop:
        break

      # Inits if not video frames
      if args.animation_mode != "Video Input":
        if args.init_image in ['','none', 'None', 'NONE']:
          init_image = None
        else:
          init_image = args.init_image
        init_scale = args.init_scale
        skip_steps = args.skip_steps
        skip_end_steps = args.skip_end_steps

      if args.animation_mode == "2D":
        if args.key_frames:
          angle = args.angle_series[frame_num]
          zoom = args.zoom_series[frame_num]
          translation_x = args.translation_x_series[frame_num]
          translation_y = args.translation_y_series[frame_num]
          print(
              f'angle: {angle}',
              f'zoom: {zoom}',
              f'translation_x: {translation_x}',
              f'translation_y: {translation_y}',
          )
        
        if frame_num > 0:
          seed += 1
          if resume_run and frame_num == start_frame:
            img_0 = cv2.imread(batchFolder+f"/{batch_name}({batchNum})_{start_frame-1:04}.png")
          else:
            img_0 = cv2.imread('prevFrame.png')
          center = (1*img_0.shape[1]//2, 1*img_0.shape[0]//2)
          trans_mat = np.float32(
              [[1, 0, translation_x],
              [0, 1, translation_y]]
          )
          rot_mat = cv2.getRotationMatrix2D( center, angle, zoom )
          trans_mat = np.vstack([trans_mat, [0,0,1]])
          rot_mat = np.vstack([rot_mat, [0,0,1]])
          transformation_matrix = np.matmul(rot_mat, trans_mat)
          img_0 = cv2.warpPerspective(
              img_0,
              transformation_matrix,
              (img_0.shape[1], img_0.shape[0]),
              borderMode=cv2.BORDER_WRAP
          )

          print("saving image 4")
          cv2.imwrite('prevFrameScaled.png', img_0)
          init_image = 'prevFrameScaled.png'
          init_scale = args.frames_scale
          skip_steps = args.calc_frames_skip_steps
          skip_end_steps = args.frames_skip_end_steps

      if args.animation_mode == "3D":
        if frame_num > 0:
          seed += 1    
          if resume_run and frame_num == start_frame:
            img_filepath = batchFolder+f"/{batch_name}({batchNum})_{start_frame-1:04}.png"
            if turbo_mode and frame_num > turbo_preroll:
              shutil.copyfile(img_filepath, 'oldFrameScaled.png')
          else:
            img_filepath = 'prevFrame.png'

          next_step_pil = do_3d_step(img_filepath, frame_num, midas_model, midas_transform)
          print("saving pil image 1")
          next_step_pil.save('prevFrameScaled.png')

          ### Turbo mode - skip some diffusions, use 3d morph for clarity and to save time
          if turbo_mode:
            if frame_num == turbo_preroll: #start tracking oldframe
              print("saving pil image 1")
              next_step_pil.save('oldFrameScaled.png')#stash for later blending          
            elif frame_num > turbo_preroll:
              #set up 2 warped image sequences, old & new, to blend toward new diff image
              old_frame = do_3d_step('oldFrameScaled.png', frame_num, midas_model, midas_transform)
              print("saving pil image 2")
              old_frame.save('oldFrameScaled.png')
              if frame_num % int(turbo_steps) != 0: 
                print('turbo skip this frame: skipping clip diffusion steps')
                filename = f'{args.batch_name}({args.batchNum})_{frame_num:04}.png'
                blend_factor = ((frame_num % int(turbo_steps))+1)/int(turbo_steps)
                print('turbo skip this frame: skipping clip diffusion steps and saving blended frame')
                newWarpedImg = cv2.imread('prevFrameScaled.png')#this is already updated..
                oldWarpedImg = cv2.imread('oldFrameScaled.png')
                blendedImage = cv2.addWeighted(newWarpedImg, blend_factor, oldWarpedImg,1-blend_factor, 0.0)
                print("saving image 1")
                cv2.imwrite(f'{batchFolder}/{filename}',blendedImage)
                print("saving pil image 3")
                next_step_pil.save(f'{img_filepath}') # save it also as prev_frame to feed next iteration
                if vr_mode:
                  generate_eye_views(TRANSLATION_SCALE,batchFolder,filename,frame_num,midas_model, midas_transform)
                continue
              else:
                #if not a skip frame, will run diffusion and need to blend.
                oldWarpedImg = cv2.imread('prevFrameScaled.png')
                print("saving image 2")
                cv2.imwrite(f'oldFrameScaled.png',oldWarpedImg)#swap in for blending later 
                print('clip/diff this frame - generate clip diff image')

          init_image = 'prevFrameScaled.png'
          init_scale = args.frames_scale
          skip_steps = args.calc_frames_skip_steps
          skip_end_steps = args.frames_skip_end_steps

      if  args.animation_mode == "Video Input":
        if not video_init_seed_continuity:
          seed += 1
        init_image = f'{videoFramesFolder}/{frame_num+1:04}.jpg'
        init_scale = args.frames_scale
        skip_steps = args.calc_frames_skip_steps
        skip_end_steps = args.skip_end_steps

      loss_values = []
  
      if seed is not None:
          np.random.seed(seed)
          random.seed(seed)
          torch.manual_seed(seed)
          torch.cuda.manual_seed_all(seed)
          torch.backends.cudnn.deterministic = True
  
      target_embeds, weights = [], []
      
      frame_prompt = [ _prompt ]
      
      print(args.image_prompts_series)
      if args.image_prompts_series is not None and frame_num >= len(args.image_prompts_series):
        image_prompt = args.image_prompts_series[-1]
      elif args.image_prompts_series is not None:
        image_prompt = args.image_prompts_series[frame_num]
      else:
        image_prompt = []

      print(f'Frame {frame_num} Prompt: {frame_prompt}')

      model_stats = []
      for clip_model in clip_models:
            cutn = 16
            model_stat = {"clip_model":None,"target_embeds":[],"make_cutouts":None,"weights":[]}
            model_stat["clip_model"] = clip_model
            
            for prompt in frame_prompt:
                txt, weight = parse_prompt(prompt)
                txt = clip_model.encode_text(clip.tokenize(prompt).to(device)).float()
                
                if args.fuzzy_prompt:
                    for i in range(25):
                        model_stat["target_embeds"].append((txt + torch.randn(txt.shape).cuda() * args.rand_mag).clamp(0,1))
                        model_stat["weights"].append(weight)
                else:
                    model_stat["target_embeds"].append(txt)
                    model_stat["weights"].append(weight)
        
            if image_prompt:
              model_stat["make_cutouts"] = MakeCutouts(clip_model.visual.input_resolution, cutn, skip_augs=skip_augs) 
              for prompt in image_prompt:
                  path, weight = parse_prompt(prompt)
                  img = Image.open(fetch(path)).convert('RGB')
                  img = TF.resize(img, min(side_x, side_y, *img.size), T.InterpolationMode.LANCZOS)
                  batch = model_stat["make_cutouts"](TF.to_tensor(img).to(device).unsqueeze(0).mul(2).sub(1))
                  embed = clip_model.encode_image(normalize(batch)).float()
                  if fuzzy_prompt:
                      for i in range(25):
                          model_stat["target_embeds"].append((embed + torch.randn(embed.shape).cuda() * rand_mag).clamp(0,1))
                          weights.extend([weight / cutn] * cutn)
                  else:
                      model_stat["target_embeds"].append(embed)
                      model_stat["weights"].extend([weight / cutn] * cutn)
        
            model_stat["target_embeds"] = torch.cat(model_stat["target_embeds"])
            model_stat["weights"] = torch.tensor(model_stat["weights"], device=device)
            if model_stat["weights"].sum().abs() < 1e-3:
                raise RuntimeError('The weights must not sum to 0.')
            model_stat["weights"] /= model_stat["weights"].sum().abs()
            model_stats.append(model_stat)
  
      init = None
      if init_image is not None:
          init = Image.open(fetch(init_image)).convert('RGB')
          #zippy noise injector csa
          if args.noise_injector == True and args.animation_mode == "3D":
            init_to_noise = cv2.imread(init_image)
            if args.copy_palette == True:
              palette_source_img = cv2.imread(args.copy_palette_image)
              init_to_noise = exposure.match_histograms(init_to_noise, palette_source_img, multichannel=True) 
            edge_mix = get_edge_mix(center_crop(init_to_noise,.33))
            print('edge mix is...',round(edge_mix,1))
            if edge_mix < args.noise_injector_threshold:
              init_to_noise = add_noise(init_to_noise,args.noise_injector_mix,args.noise_injector_px_size,
                                        args.noise_injector_spice,args.noise_injector_mask_feather,
                                        args.noise_injector_sigma) 
              print('edge mix was below threshold, noise was added')
              #ratio = np.amax(init_to_noise) / 256       
              #converted_noised_init = (init_to_noise / ratio).astype('uint8')
              #recolored_noised_init = cv2.cvtColor(converted_noised_init,cv2.COLOR_BGR2RGB)
              #init = Image.fromarray(recolored_noised_init)
            else:
              print('edge mix was above threshold, no noise was added')
            #now convert init_to_noise back to PIL img format
            ratio = np.amax(init_to_noise) / 256       
            converted_noised_init = (init_to_noise / ratio).astype('uint8')
            recolored_noised_init = cv2.cvtColor(converted_noised_init,cv2.COLOR_BGR2RGB)
            init = Image.fromarray(recolored_noised_init)         
          #end noise injector
          init = init.resize((args.side_x, args.side_y), Image.LANCZOS)
          init = TF.to_tensor(init).to(device).unsqueeze(0).mul(2).sub(1)
      
      if args.perlin_init:
          if args.perlin_mode == 'color':
              init = create_perlin_noise([1.5**-i*0.5 for i in range(12)], 1, 1, False)
              init2 = create_perlin_noise([1.5**-i*0.5 for i in range(8)], 4, 4, False)
          elif args.perlin_mode == 'gray':
            init = create_perlin_noise([1.5**-i*0.5 for i in range(12)], 1, 1, True)
            init2 = create_perlin_noise([1.5**-i*0.5 for i in range(8)], 4, 4, True)
          else:
            init = create_perlin_noise([1.5**-i*0.5 for i in range(12)], 1, 1, False)
            init2 = create_perlin_noise([1.5**-i*0.5 for i in range(8)], 4, 4, True)
          # init = TF.to_tensor(init).add(TF.to_tensor(init2)).div(2).to(device)
          init = TF.to_tensor(init).add(TF.to_tensor(init2)).div(2).to(device).unsqueeze(0).mul(2).sub(1)
          del init2
  
      cur_t = None
  
      def cond_fn(x, t, y=None):
          with torch.enable_grad():
              x_is_NaN = False
              x = x.detach().requires_grad_()
              n = x.shape[0]
              if use_secondary_model is True:
                alpha = torch.tensor(diffusion.sqrt_alphas_cumprod[cur_t], device=device, dtype=torch.float32)
                sigma = torch.tensor(diffusion.sqrt_one_minus_alphas_cumprod[cur_t], device=device, dtype=torch.float32)
                cosine_t = alpha_sigma_to_t(alpha, sigma)
                out = secondary_model(x, cosine_t[None].repeat([n])).pred
                fac = diffusion.sqrt_one_minus_alphas_cumprod[cur_t]
                x_in = out * fac + x * (1 - fac)
                x_in_grad = torch.zeros_like(x_in)
              else:
                my_t = torch.ones([n], device=device, dtype=torch.long) * cur_t
                out = diffusion.p_mean_variance(model, x, my_t, clip_denoised=False, model_kwargs={'y': y})
                fac = diffusion.sqrt_one_minus_alphas_cumprod[cur_t]
                x_in = out['pred_xstart'] * fac + x * (1 - fac)
                x_in_grad = torch.zeros_like(x_in)
              for model_stat in model_stats:
                for i in range(args.cutn_batches):
                    t_int = int(t.item())+1 #errors on last step without +1, need to find source
                    #when using SLIP Base model the dimensions need to be hard coded to avoid AttributeError: 'VisionTransformer' object has no attribute 'input_resolution'
                    try:
                        input_resolution=model_stat["clip_model"].visual.input_resolution
                    except:
                        input_resolution=224

                    cuts = MakeCutoutsDango(input_resolution,
                            Overview= args.cut_overview[1000-t_int], 
                            InnerCrop = args.cut_innercut[1000-t_int], 
                            IC_Size_Pow=args.cut_ic_pow[1000-t_int], 
                            IC_Grey_P = args.cut_icgray_p[1000-t_int]
                            )
                    clip_in = normalize(cuts(x_in.add(1).div(2)))
                    image_embeds = model_stat["clip_model"].encode_image(clip_in).float()
                    dists = spherical_dist_loss(image_embeds.unsqueeze(1), model_stat["target_embeds"].unsqueeze(0))
                    dists = dists.view([args.cut_overview[1000-t_int]+args.cut_innercut[1000-t_int], n, -1])
                    losses = dists.mul(model_stat["weights"]).sum(2).mean(0)
                    loss_values.append(losses.sum().item()) # log loss, probably shouldn't do per cutn_batch
                    x_in_grad += torch.autograd.grad(losses.sum() * clip_guidance_scale, x_in)[0] / cutn_batches
              tv_losses = tv_loss(x_in)
              if use_secondary_model is True:
                range_losses = range_loss(out)
              else:
                range_losses = range_loss(out['pred_xstart'])
              #sat_losses = torch.abs(x_in - x_in.clamp(min=-1,max=1)).mean()
              #updated sat_losses function.. csa uses sat_scale_buffer
              sat_losses = torch.abs(x_in - x_in.clamp(min=-1+args.sat_scale_buffer,max=1-args.sat_scale_buffer)).mean()
              if sat_scale_buffer > 0:
                sat_losses = (sat_losses/sat_scale_buffer)**2 # 
                sat_losses = sat_losses.clamp(min=0,max=2)#limit to 2 max to avoid nonlinearity
              loss = tv_losses.sum() * tv_scale + range_losses.sum() * range_scale + sat_losses.sum() * sat_scale
              if init is not None and init_scale:
                  init_losses = lpips_model(x_in, init)
                  loss = loss + init_losses.sum() * init_scale
              x_in_grad += torch.autograd.grad(loss, x_in)[0]
              if torch.isnan(x_in_grad).any()==False:
                  grad = -torch.autograd.grad(x_in, x, x_in_grad)[0]
              else:
                # print("NaN'd")
                x_is_NaN = True
                grad = torch.zeros_like(x)
          if args.clamp_grad and x_is_NaN == False:
              magnitude = grad.square().mean().sqrt()
              return grad * magnitude.clamp(max=args.clamp_max) / magnitude  #min=-0.02, min=-clamp_max, 
          return grad
  
      if args.diffusion_sampling_mode == 'ddim':
          sample_fn = diffusion.ddim_sample_loop_progressive
      else:
          sample_fn = diffusion.plms_sample_loop_progressive

      for i in range(args.n_batches):
          print('')
          gc.collect()
          torch.cuda.empty_cache()
          cur_t = diffusion.num_timesteps - skip_steps - 1
          total_steps = cur_t
          cur_t -= skip_end_steps

          if perlin_init:
              init = regen_perlin()

          if args.diffusion_sampling_mode == 'ddim':
              samples = sample_fn(
                  model,
                  (batch_size, 3, args.side_y, args.side_x),
                  clip_denoised=clip_denoised,
                  model_kwargs={},
                  cond_fn=cond_fn,
                  progress=True,
                  skip_timesteps=skip_steps,
                  init_image=init,
                  randomize_class=randomize_class,
                  eta=eta,
              )
          else:
              samples = sample_fn(
                  model,
                  (batch_size, 3, args.side_y, args.side_x),
                  clip_denoised=clip_denoised,
                  model_kwargs={},
                  cond_fn=cond_fn,
                  progress=True,
                  skip_timesteps=skip_steps,
                  init_image=init,
                  randomize_class=randomize_class,
                  order=2,
              )
          
          for j, sample in enumerate(samples):    
              OUTPUT_FILENAME = datetime.now().strftime("%Y%m%d-%H%M%S") + "_" + str(random.randint(1000, 9999)) 
              cur_t -= 1
              intermediateStep = False
              if args.steps_per_checkpoint is not None:
                  if j % steps_per_checkpoint == 0 and j > 0:
                    intermediateStep = True
              elif j in args.intermediate_saves:
                intermediateStep = True
              #with image_display:
              
              if j % args.display_rate == 0 or cur_t == -1 or intermediateStep == True:
                  for k, image in enumerate(sample['pred_xstart']):
                      # tqdm.write(f'Batch {i}, step {j}, output {k}:')
                      current_time = datetime.now().strftime('%y%m%d-%H%M%S_%f')
                      percent = math.ceil(j/total_steps*100)
                      if args.n_batches > 0:
                        #if intermediates are saved to the subfolder, don't append a step or percentage to the name
                        if cur_t == -1 and args.intermediates_in_subfolder is True:
                          save_num = f'{frame_num:04}' if animation_mode != "None" else i
                          filename = f'{args.batch_name}({args.batchNum})_{save_num}.png'
                          print("setting filename 1")
                        else:
                          #If we're working with percentages, append it
                          if args.steps_per_checkpoint is not None:
                            filename = f'{args.batch_name}({args.batchNum})_{i:04}-{percent:02}%.png'
                            print("setting filename 2")
                          # Or else, iIf we're working with specific steps, append those
                          else:
                            print("setting filename 3")
                            filename = f'{args.batch_name}({args.batchNum})_{i:04}-{j:03}.png'
                      #Optional Soft Limiter
                      if args.soft_limiter_on == True:
                          #zippy's soft limiter
                          image=(soft_limit(image)+1)/2
                          image = TF.to_pil_image(image)
                      else:
                          #default clamping behavior
                          #clamp values to between 0 and 1
                          print("dada")
                          image = TF.to_pil_image(image.add(1).div(2).clamp(0, 1))
                      if cur_t == -1:
                        #if frame_num == 0:
                        #  save_settings(seed=args.seed)
                        print("saving pil image dd")
                        image.save(f'{batchFolder}/{filename}')  
              if cur_t < -1 : 
                print("breaking, due to cur_t")
                break # this is mainly used to account for skip_end_steps
  return batchFolder, batch_name, batchNum

def generate_eye_views(trans_scale,batchFolder,filename,frame_num,midas_model, midas_transform):
   for i in range(2):
      theta = vr_eye_angle * (math.pi/180)
      ray_origin = math.cos(theta) * vr_ipd / 2 * (-1.0 if i==0 else 1.0)
      ray_rotation = (theta if i==0 else -theta)
      translate_xyz = [-(ray_origin)*trans_scale, 0,0]
      rotate_xyz = [0, (ray_rotation), 0]
      rot_mat = p3dT.euler_angles_to_matrix(torch.tensor(rotate_xyz, device=device), "XYZ").unsqueeze(0)
      transformed_image = dxf.transform_image_3d(f'{batchFolder}/{filename}', midas_model, midas_transform, DEVICE,
                                                      rot_mat, translate_xyz, args.near_plane, args.far_plane,
                                                      args.fov, padding_mode=args.padding_mode,
                                                      sampling_mode=args.sampling_mode, midas_weight=args.midas_weight,spherical=True)
      eye_file_path = batchFolder+f"/frame_{frame_num:04}" + ('_l' if i==0 else '_r')+'.png'
      print("saving pil image 6")
      transformed_image.save(eye_file_path)

#@title 1.6 Define the secondary diffusion model

def append_dims(x, n):
    return x[(Ellipsis, *(None,) * (n - x.ndim))]


def expand_to_planes(x, shape):
    return append_dims(x, len(shape)).repeat([1, 1, *shape[2:]])


def alpha_sigma_to_t(alpha, sigma):
    return torch.atan2(sigma, alpha) * 2 / math.pi


def t_to_alpha_sigma(t):
    return torch.cos(t * math.pi / 2), torch.sin(t * math.pi / 2)


@dataclass
class DiffusionOutput:
    v: torch.Tensor
    pred: torch.Tensor
    eps: torch.Tensor


class ConvBlock(nn.Sequential):
    def __init__(self, c_in, c_out):
        super().__init__(
            nn.Conv2d(c_in, c_out, 3, padding=1),
            nn.ReLU(inplace=True),
        )


class SkipBlock(nn.Module):
    def __init__(self, main, skip=None):
        super().__init__()
        self.main = nn.Sequential(*main)
        self.skip = skip if skip else nn.Identity()

    def forward(self, input):
        return torch.cat([self.main(input), self.skip(input)], dim=1)


class FourierFeatures(nn.Module):
    def __init__(self, in_features, out_features, std=1.):
        super().__init__()
        assert out_features % 2 == 0
        self.weight = nn.Parameter(torch.randn([out_features // 2, in_features]) * std)

    def forward(self, input):
        f = 2 * math.pi * input @ self.weight.T
        return torch.cat([f.cos(), f.sin()], dim=-1)


class SecondaryDiffusionImageNet(nn.Module):
    def __init__(self):
        super().__init__()
        c = 64  # The base channel count

        self.timestep_embed = FourierFeatures(1, 16)

        self.net = nn.Sequential(
            ConvBlock(3 + 16, c),
            ConvBlock(c, c),
            SkipBlock([
                nn.AvgPool2d(2),
                ConvBlock(c, c * 2),
                ConvBlock(c * 2, c * 2),
                SkipBlock([
                    nn.AvgPool2d(2),
                    ConvBlock(c * 2, c * 4),
                    ConvBlock(c * 4, c * 4),
                    SkipBlock([
                        nn.AvgPool2d(2),
                        ConvBlock(c * 4, c * 8),
                        ConvBlock(c * 8, c * 4),
                        nn.Upsample(scale_factor=2, mode='bilinear', align_corners=False),
                    ]),
                    ConvBlock(c * 8, c * 4),
                    ConvBlock(c * 4, c * 2),
                    nn.Upsample(scale_factor=2, mode='bilinear', align_corners=False),
                ]),
                ConvBlock(c * 4, c * 2),
                ConvBlock(c * 2, c),
                nn.Upsample(scale_factor=2, mode='bilinear', align_corners=False),
            ]),
            ConvBlock(c * 2, c),
            nn.Conv2d(c, 3, 3, padding=1),
        )

    def forward(self, input, t):
        timestep_embed = expand_to_planes(self.timestep_embed(t[:, None]), input.shape)
        v = self.net(torch.cat([input, timestep_embed], dim=1))
        alphas, sigmas = map(partial(append_dims, n=v.ndim), t_to_alpha_sigma(t))
        pred = input * alphas - v * sigmas
        eps = input * sigmas + v * alphas
        return DiffusionOutput(v, pred, eps)


class SecondaryDiffusionImageNet2(nn.Module):
    def __init__(self):
        super().__init__()
        c = 64  # The base channel count
        cs = [c, c * 2, c * 2, c * 4, c * 4, c * 8]

        self.timestep_embed = FourierFeatures(1, 16)
        self.down = nn.AvgPool2d(2)
        self.up = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=False)

        self.net = nn.Sequential(
            ConvBlock(3 + 16, cs[0]),
            ConvBlock(cs[0], cs[0]),
            SkipBlock([
                self.down,
                ConvBlock(cs[0], cs[1]),
                ConvBlock(cs[1], cs[1]),
                SkipBlock([
                    self.down,
                    ConvBlock(cs[1], cs[2]),
                    ConvBlock(cs[2], cs[2]),
                    SkipBlock([
                        self.down,
                        ConvBlock(cs[2], cs[3]),
                        ConvBlock(cs[3], cs[3]),
                        SkipBlock([
                            self.down,
                            ConvBlock(cs[3], cs[4]),
                            ConvBlock(cs[4], cs[4]),
                            SkipBlock([
                                self.down,
                                ConvBlock(cs[4], cs[5]),
                                ConvBlock(cs[5], cs[5]),
                                ConvBlock(cs[5], cs[5]),
                                ConvBlock(cs[5], cs[4]),
                                self.up,
                            ]),
                            ConvBlock(cs[4] * 2, cs[4]),
                            ConvBlock(cs[4], cs[3]),
                            self.up,
                        ]),
                        ConvBlock(cs[3] * 2, cs[3]),
                        ConvBlock(cs[3], cs[2]),
                        self.up,
                    ]),
                    ConvBlock(cs[2] * 2, cs[2]),
                    ConvBlock(cs[2], cs[1]),
                    self.up,
                ]),
                ConvBlock(cs[1] * 2, cs[1]),
                ConvBlock(cs[1], cs[0]),
                self.up,
            ]),
            ConvBlock(cs[0] * 2, cs[0]),
            nn.Conv2d(cs[0], 3, 3, padding=1),
        )

    def forward(self, input, t):
        timestep_embed = expand_to_planes(self.timestep_embed(t[:, None]), input.shape)
        v = self.net(torch.cat([input, timestep_embed], dim=1))
        alphas, sigmas = map(partial(append_dims, n=v.ndim), t_to_alpha_sigma(t))
        pred = input * alphas - v * sigmas
        eps = input * sigmas + v * alphas
        return DiffusionOutput(v, pred, eps)

"""# 2. Diffusion and CLIP model settings"""

#@markdown ####**Models Settings:**
diffusion_model = "spritesheetdiffusion" #@param ["spritesheetdiffusion"]

#@markdown If you enable the secondary model, beware: secondary *REALLY* likes making everything it can into buildings.
use_secondary_model = False #@param {type: 'boolean'}
diffusion_sampling_mode = 'ddim' #@param ['plms','ddim'] 


#@markdown #####**CLIP settings:**
ViTB32 = True #@param{type:"boolean"}
ViTB16 = True #@param{type:"boolean"}
ViTL14 = True #@param{type:"boolean"}
ViTL14_336 = False #@param{type:"boolean"}
RN101 = False #@param{type:"boolean"}
RN50 = True #@param{type:"boolean"}
RN50x4 = False #@param{type:"boolean"}
RN50x16 = False #@param{type:"boolean"}
RN50x64 = False #@param{type:"boolean"}

#@markdown If you're having issues with model downloads, check this to compare SHA's:
check_model_SHA = True #@param{type:"boolean"}
use_checkpoint = True #@param {type: 'boolean'}
model_spritesheetdiffusion_SHA = 'cc7162faf18f6e27b6d3f89cd40f66caa793dabca8862892e838167c9d4fa606'
model_secondary_SHA = '983e3de6f95c88c81b2ca7ebb2c217933be1973b1ff058776b970f901584613a'

model_spritesheetdiffusion_link = 'https://huggingface.co/KaliYuga/spritesheetdiffusion/resolve/main/spritesheetdiffusion.pt'
model_secondary_link = 'https://v-diffusion.s3.us-west-2.amazonaws.com/secondary_model_imagenet_2.pth'

model_spritesheetdiffusion_path = f'{model_path}/spritesheetdiffusion.pt'
model_secondary_path = f'{model_path}/secondary_model_imagenet_2.pth'

model_spritesheetdiffusion_downloaded = True
model_secondary_downloaded = False


# Download the diffusion model
if diffusion_model == 'spritesheetdiffusion':
  if os.path.exists(model_spritesheetdiffusion_path) and check_model_SHA:
    print('Checking Sprite Sheet Diffusion Model File')
    with open(model_spritesheetdiffusion_path,"rb") as f:
        bytes = f.read() 
        hash = hashlib.sha256(bytes).hexdigest();
    if hash == model_spritesheetdiffusion_SHA:
      print('Sprite Sheet Diffusion SHA matches')
      model_spritesheetdiffusion_downloaded = True

  elif os.path.exists(model_spritesheetdiffusion_path) and not check_model_SHA or model_spritesheetdiffusion_downloaded == True:
    print('Sprite Sheet Diffusion already downloaded, check check_model_SHA if the file is corrupt')


# Download the secondary diffusion model v2
if use_secondary_model == True:
  if os.path.exists(model_secondary_path) and check_model_SHA:
    print('Checking Secondary Diffusion File')
    print(model_secondary_path)
    with open(model_secondary_path,"rb") as f:
        bytes = f.read() 
        hash = hashlib.sha256(bytes).hexdigest();
    if hash == model_secondary_SHA:
      print('Secondary Model SHA matches')
      model_secondary_downloaded = True
    else:  
      print("Secondary Model SHA doesn't match, redownloading...")
      wget(model_secondary_link, model_path)
      model_secondary_downloaded = True
  elif os.path.exists(model_secondary_path) and not check_model_SHA or model_secondary_downloaded == True:
    print('Secondary Model already downloaded, check check_model_SHA if the file is corrupt')
  else:  
    wget(model_secondary_link, model_path)
    model_secondary_downloaded = True

model_config = model_and_diffusion_defaults()
if diffusion_model == '512x512_diffusion_uncond_finetune_008100':
    model_config.update({
        'attention_resolutions': '32, 16, 8',
        'class_cond': False,
        'diffusion_steps': 1000, #No need to edit this, it is taken care of later.
        'rescale_timesteps': True,
        'timestep_respacing': 250, #No need to edit this, it is taken care of later.
        'image_size': 512,
        'learn_sigma': True,
        'noise_schedule': 'linear',
        'num_channels': 256,
        'num_head_channels': 64,
        'num_res_blocks': 2,
        'resblock_updown': True,
        'use_checkpoint': use_checkpoint,
        'use_fp16': True,
        'use_scale_shift_norm': True,
    })
elif diffusion_model == '256x256_diffusion_uncond':
    model_config.update({
        'attention_resolutions': '32, 16, 8',
        'class_cond': False,
        'diffusion_steps': 1000, #No need to edit this, it is taken care of later.
        'rescale_timesteps': True,
        'timestep_respacing': 250, #No need to edit this, it is taken care of later.
        'image_size': 256,
        'learn_sigma': True,
        'noise_schedule': 'linear',
        'num_channels': 256,
        'num_head_channels': 64,
        'num_res_blocks': 2,
        'resblock_updown': True,
        'use_checkpoint': use_checkpoint,
        'use_fp16': True,
        'use_scale_shift_norm': True,
    })
elif diffusion_model == 'spritesheetdiffusion':
    model_config.update({
          'attention_resolutions': '16',
          'class_cond': False,
          'diffusion_steps': 1000,
          'rescale_timesteps': True,
          'timestep_respacing': 'ddim100',
          'image_size': 256,
          'learn_sigma': True,
          'noise_schedule': 'linear',
          'num_channels': 128,
          'num_heads': 1,
          'num_res_blocks': 2,
          'use_checkpoint': use_checkpoint,
          'use_fp16': True,
          'use_scale_shift_norm': False,
      })
elif diffusion_model == 'pixel_art_diffusion_soft_256':
    model_config.update({
          'attention_resolutions': '16',
          'class_cond': False,
          'diffusion_steps': 1000,
          'rescale_timesteps': True,
          'timestep_respacing': 'ddim100',
          'image_size': 256,
          'learn_sigma': True,
          'noise_schedule': 'linear',
          'num_channels': 128,
          'num_heads': 1,
          'num_res_blocks': 2,
          'use_checkpoint': use_checkpoint,
          'use_fp16': True,
          'use_scale_shift_norm': False,
      })
elif diffusion_model == 'pixelartdiffusion4k':
    model_config.update({
          'attention_resolutions': '16',
          'class_cond': False,
          'diffusion_steps': 1000,
          'rescale_timesteps': True,
          'timestep_respacing': 'ddim100',
          'image_size': 256,
          'learn_sigma': True,
          'noise_schedule': 'linear',
          'num_channels': 128,
          'num_heads': 1,
          'num_res_blocks': 2,
          'use_checkpoint': use_checkpoint,
          'use_fp16': True,
          'use_scale_shift_norm': False,
      })
elif diffusion_model == 'PADexpanded':
    model_config.update({
          'attention_resolutions': '16',
          'class_cond': False,
          'diffusion_steps': 1000,
          'rescale_timesteps': True,
          'timestep_respacing': 'ddim100',
          'image_size': 256,
          'learn_sigma': True,
          'noise_schedule': 'linear',
          'num_channels': 128,
          'num_heads': 1,
          'num_res_blocks': 2,
          'use_checkpoint': use_checkpoint,
          'use_fp16': True,
          'use_scale_shift_norm': False,
      })
model_default = model_config['image_size']

if use_secondary_model:
    secondary_model = SecondaryDiffusionImageNet2()
    secondary_model.load_state_dict(torch.load(f'{model_path}/secondary_model_imagenet_2.pth', map_location='cpu'))
    secondary_model.eval().requires_grad_(False).to(device)

clip_models = []
if ViTB32 is True: clip_models.append(clip.load(os.path.join(model_path,'ViT-B-32.pt'), jit=False)[0].eval().requires_grad_(False).to(device)) 
if ViTB16 is True: clip_models.append(clip.load(os.path.join(model_path,'ViT-B-16.pt'), jit=False)[0].eval().requires_grad_(False).to(device) ) 
if ViTL14 is True: clip_models.append(clip.load(os.path.join(model_path,'ViT-L-14.pt'), jit=False)[0].eval().requires_grad_(False).to(device) ) 
if RN50 is True: clip_models.append(clip.load(os.path.join(model_path,'RN50.pt'), jit=False)[0].eval().requires_grad_(False).to(device))
if RN50x4 is True: clip_models.append(clip.load('RN50x4', jit=False)[0].eval().requires_grad_(False).to(device)) 
if RN50x16 is True: clip_models.append(clip.load('RN50x16', jit=False)[0].eval().requires_grad_(False).to(device)) 
if RN50x64 is True: clip_models.append(clip.load('RN50x64', jit=False)[0].eval().requires_grad_(False).to(device)) 
if RN101 is True: clip_models.append(clip.load('RN101', jit=False)[0].eval().requires_grad_(False).to(device)) 

normalize = T.Normalize(mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711])
lpips_model = lpips.LPIPS(pretrained = True, net='vgg', model_path = os.path.join(model_path, 'vgg.pth')).to(device)

"""# 3. Settings"""

#@markdown ####**Basic Settings:**
batch_name = 'Sprite_Sheet_Diffusion' #@param{type: 'string'}
steps =  100#@param [25,50,100,150,250,500,1000]{type: 'raw', allow-input: true}
width_height = [256, 256]#@param{type: 'raw'}
clip_guidance_scale = 15000 #@param{type: 'number'}
tv_scale =  0#@param{type: 'number'}
range_scale =   350#@param{type: 'number'}
sat_scale =   40000#@param{type: 'number'}
sat_scale_buffer =   .05#@param{type: 'number'}
if sat_scale_buffer < 0.0 or sat_scale_buffer > .2:
  sat_scale_buffer = 0
  print('sat_scale_buffer out of range. Automatically reset to 0.0')
cutn_batches =   1#@param{type: 'number'}
skip_augs = False#@param{type: 'boolean'}

#@markdown ---
#@markdown ####**Soft Limiter (Use 0.97 - 0.995 range):**
#@markdown *Experimental! ...may help mitigate color clipping.*
soft_limiter_on = False#@param{type: 'boolean'}
soft_limiter_knee = .98 #@param{type: 'number'}
if soft_limiter_knee < 0.5 or soft_limiter_knee > .999:
  soft_limiter_knee = .98
  print('soft_limiter_knee out of range. Automatically reset to 0.98')

#@markdown ---
#@markdown ####**Init Settings:**
init_image = None #@param{type: 'string'}
init_scale = 1000 #@param{type: 'integer'}
skip_steps =  0#@param{type: 'integer'}
#@markdown *Make sure you set skip_steps to ~50% of your steps if you want to use an init image.*

#@markdown ---
skip_end_steps =  0#@param{type: 'integer'}
#@markdown Will give an unfinished touch to your result, depending on how many end steps you want to skip.

#Get corrected sizes
side_x = (width_height[0]//64)*64;
side_y = (width_height[1]//64)*64;
if side_x != width_height[0] or side_y != width_height[1]:
    print(f'Changing output size to {side_x}x{side_y}. Dimensions must by multiples of 64.')

#Update Model Settings
timestep_respacing = f'ddim{steps}'
diffusion_steps = (1000//steps)*steps if steps < 1000 else steps
model_config.update({
    'timestep_respacing': timestep_respacing,
    'diffusion_steps': diffusion_steps,
})

"""### Animation Settings"""

#@markdown ####**Animation Mode:**
animation_mode = 'None' #@param ['None', '2D', '3D', 'Video Input'] {type:'string'}
#@markdown *For animation, you probably want to turn `cutn_batches` to 1 to make it quicker.*


#@markdown ---

#@markdown ####**Video Input Settings:**
video_init_path = "training.mp4" #@param {type: 'string'}
extract_nth_frame = 2 #@param {type: 'number'}
video_init_seed_continuity = True #@param {type: 'boolean'}

if animation_mode == "Video Input":
  videoFramesFolder = f'videoFrames'
  createPath(videoFramesFolder)
  print(f"Exporting Video Frames (1 every {extract_nth_frame})...")
  try:
      for f in pathlib.Path(f'{videoFramesFolder}').glob('*.jpg'):
          f.unlink()
  except:
      print('')
  vf = f'select=not(mod(n\,{extract_nth_frame}))'
  subprocess.run(['ffmpeg', '-i', f'{video_init_path}', '-vf', f'{vf}', '-vsync', 'vfr', '-q:v', '2', '-loglevel', 'error', '-stats', f'{videoFramesFolder}/%04d.jpg'], stdout=subprocess.PIPE).stdout.decode('utf-8')
  #!ffmpeg -i {video_init_path} -vf {vf} -vsync vfr -q:v 2 -loglevel error -stats {videoFramesFolder}/%04d.jpg


#@markdown ---

#@markdown ####**2D Animation Settings:**
#@markdown `zoom` is a multiplier of dimensions, 1 is no zoom.
#@markdown All rotations are provided in degrees.

key_frames = True #@param {type:"boolean"}
max_frames = 10000#@param {type:"number"}

if animation_mode == "Video Input":
    max_frames = len(glob(f'{videoFramesFolder}/*.jpg'))

interp_spline = 'Linear' #Do not change, currently will not look good. param ['Linear','Quadratic','Cubic']{type:"string"}
angle = "0:(0)"#@param {type:"string"}
zoom = "0: (1.0), 10: (1.05)"#@param {type:"string"}
translation_x = "0: (0)"#@param {type:"string"}
translation_y = "0: (0)"#@param {type:"string"}
translation_z = "0: (10.0)"#@param {type:"string"}
rotation_3d_x = "0: (0)"#@param {type:"string"}
rotation_3d_y = "0: (0)"#@param {type:"string"}
rotation_3d_z = "0: (0)"#@param {type:"string"}
midas_depth_model = "dpt_large"#@param {type:"string"}
midas_weight = 0.3#@param {type:"number"}
near_plane = 100#@param {type:"number"}
far_plane = 10000#@param {type:"number"}
fov = 120#@param {type:"number"}
padding_mode = 'border'#@param {type:"string"}
sampling_mode = 'bicubic'#@param {type:"string"}

#======= TURBO MODE
#@markdown ---
#@markdown ####**Turbo Mode (3D anim only):**
#@markdown (Starts after frame 10,) skips diffusion steps and just uses depth map to warp images for skipped frames.
#@markdown Speeds up rendering by 2x-4x, and may improve image coherence between frames. frame_blend_mode smooths abrupt texture changes across 2 frames.
#@markdown For different settings tuned for Turbo Mode, refer to the original Disco-Turbo Github: https://github.com/zippy731/disco-diffusion-turbo

turbo_mode = True #@param {type:"boolean"}
turbo_steps = "3" #@param ["2","3","4","5","6"] {type:"string"}
turbo_preroll = 10 # frames

#insist turbo be used only w 3d anim.
if turbo_mode and animation_mode != '3D':
    print('=====')
    print('Turbo mode only available with 3D animations. Disabling Turbo.')
    print('=====')
    turbo_mode = False

#@markdown ---

#@markdown ####**Coherency Settings:**
#@markdown `frame_scale` tries to guide the new frame to looking like the old one. A good default is 1500.
frames_scale = 0 #@param{type: 'integer'}
#@markdown `frame_skip_steps` will blur the previous frame - higher values will flicker less but struggle to add enough new detail to zoom into.
frames_skip_steps = '70%' #@param ['40%', '50%', '60%', '65%', '70%', '75%','80%'] {type: 'string'}
#@markdown `frames_skip_end_steps` will skip the tail end of the diffusion - higher values will leave final frame with slightly less detail.
frames_skip_end_steps = 0 #@param{type: 'integer'}

#============= NOISE INJECTOR

#@markdown ---
#@markdown ####**Noise Injector (3D anim only):**
#@markdown (experimental!) Adds noise if dead spots detected
noise_injector = False #@param {type:"boolean"}
noise_injector_threshold =  50#@param{type: 'integer'}
#@markdown `noise_injector_threshold` if edge% falls below this %, will boost next frame. 20 is a good starting value.
noise_injector_mix = 0.10 #@param{type: 'number'}
#@markdown `noise_injector_mix` overall wet/dry mix for noise effect 0 - 1.0 
noise_injector_px_size =  1#@param{type: 'integer'}
#@markdown `noise_injector_px_size` size of noise color blocks.
noise_injector_spice = .10 #@param{type: 'number'}
#@markdown `noise_injector_spice` adds random noise to color-matched noise. 0 - 1.0 ratio.
noise_injector_mask_feather = 10 #@param{type: 'integer'}
#@markdown `noise_injector_mask_feather` introduce soft edges at noise mask distance in px. 
noise_injector_sigma = 32 #@param{type: 'integer'}
#@markdown `noise_injector_sigma` = size of 'clumps' in noise. higher is larger, 16-32 are good starting pt 

#@markdown ####**Copy palette conform animation frames to color mix from palette_image (3D anim /noise inj only):**
copy_palette = False#@param{type: 'boolean'}
copy_palette_image = "" #@param{type: 'string'}


#======= VR MODE
#@markdown ---
#@markdown ####**VR Mode (3D anim only):**
#@markdown Enables stereo rendering of left/right eye views (supporting Turbo) which use a different (fish-eye) camera projection matrix.   
#@markdown Note the images you're prompting will work better if they have some inherent wide-angle aspect
#@markdown The generated images will need to be combined into left/right videos. These can then be stitched into the VR180 format.
#@markdown Google made the VR180 Creator tool but subsequently stopped supporting it. It's available for download in a few places including https://www.patrickgrunwald.de/vr180-creator-download
#@markdown The tool is not only good for stitching (videos and photos) but also for adding the correct metadata into existing videos, which is needed for services like YouTube to identify the format correctly.
#@markdown Watching YouTube VR videos isn't necessarily the easiest depending on your headset. For instance Oculus have a dedicated media studio and store which makes the files easier to access on a Quest https://creator.oculus.com/manage/mediastudio/
#@markdown 
#@markdown The command to get ffmpeg to concat your frames for each eye is in the form: `ffmpeg -framerate 15 -i frame_%4d_l.png l.mp4` (repeat for r)

vr_mode = False #@param {type:"boolean"}
#@markdown `vr_eye_angle` is the y-axis rotation of the eyes towards the center
vr_eye_angle = 0.5 #@param{type:"number"}
#@markdown interpupillary distance (between the eyes)
vr_ipd = 5.0 #@param{type:"number"}

#insist VR be used only w 3d anim.
if vr_mode and animation_mode != '3D':
    print('=====')
    print('VR mode only available with 3D animations. Disabling VR.')
    print('=====')
    vr_mode = False


def parse_key_frames(string, prompt_parser=None):
    """Given a string representing frame numbers paired with parameter values at that frame,
    return a dictionary with the frame numbers as keys and the parameter values as the values.

    Parameters
    ----------
    string: string
        Frame numbers paired with parameter values at that frame number, in the format
        'framenumber1: (parametervalues1), framenumber2: (parametervalues2), ...'
    prompt_parser: function or None, optional
        If provided, prompt_parser will be applied to each string of parameter values.
    
    Returns
    -------
    dict
        Frame numbers as keys, parameter values at that frame number as values

    Raises
    ------
    RuntimeError
        If the input string does not match the expected format.
    
    Examples
    --------
    >>> parse_key_frames("10:(Apple: 1| Orange: 0), 20: (Apple: 0| Orange: 1| Peach: 1)")
    {10: 'Apple: 1| Orange: 0', 20: 'Apple: 0| Orange: 1| Peach: 1'}

    >>> parse_key_frames("10:(Apple: 1| Orange: 0), 20: (Apple: 0| Orange: 1| Peach: 1)", prompt_parser=lambda x: x.lower()))
    {10: 'apple: 1| orange: 0', 20: 'apple: 0| orange: 1| peach: 1'}
    """
    import re
    pattern = r'((?P<frame>[0-9]+):[\s]*[\(](?P<param>[\S\s]*?)[\)])'
    frames = dict()
    for match_object in re.finditer(pattern, string):
        frame = int(match_object.groupdict()['frame'])
        param = match_object.groupdict()['param']
        if prompt_parser:
            frames[frame] = prompt_parser(param)
        else:
            frames[frame] = param

    if frames == {} and len(string) != 0:
        raise RuntimeError('Key Frame string not correctly formatted')
    return frames

def get_inbetweens(key_frames, integer=False):
    """Given a dict with frame numbers as keys and a parameter value as values,
    return a pandas Series containing the value of the parameter at every frame from 0 to max_frames.
    Any values not provided in the input dict are calculated by linear interpolation between
    the values of the previous and next provided frames. If there is no previous provided frame, then
    the value is equal to the value of the next provided frame, or if there is no next provided frame,
    then the value is equal to the value of the previous provided frame. If no frames are provided,
    all frame values are NaN.

    Parameters
    ----------
    key_frames: dict
        A dict with integer frame numbers as keys and numerical values of a particular parameter as values.
    integer: Bool, optional
        If True, the values of the output series are converted to integers.
        Otherwise, the values are floats.
    
    Returns
    -------
    pd.Series
        A Series with length max_frames representing the parameter values for each frame.
    
    Examples
    --------
    >>> max_frames = 5
    >>> get_inbetweens({1: 5, 3: 6})
    0    5.0
    1    5.0
    2    5.5
    3    6.0
    4    6.0
    dtype: float64

    >>> get_inbetweens({1: 5, 3: 6}, integer=True)
    0    5
    1    5
    2    5
    3    6
    4    6
    dtype: int64
    """
    key_frame_series = pd.Series([np.nan for a in range(max_frames)])

    for i, value in key_frames.items():
        key_frame_series[i] = value
    key_frame_series = key_frame_series.astype(float)
    
    interp_method = interp_spline

    if interp_method == 'Cubic' and len(key_frames.items()) <=3:
      interp_method = 'Quadratic'
    
    if interp_method == 'Quadratic' and len(key_frames.items()) <= 2:
      interp_method = 'Linear'
      
    
    key_frame_series[0] = key_frame_series[key_frame_series.first_valid_index()]
    key_frame_series[max_frames-1] = key_frame_series[key_frame_series.last_valid_index()]
    # key_frame_series = key_frame_series.interpolate(method=intrp_method,order=1, limit_direction='both')
    key_frame_series = key_frame_series.interpolate(method=interp_method.lower(),limit_direction='both')
    if integer:
        return key_frame_series.astype(int)
    return key_frame_series

def split_prompts(prompts):
    prompt_series = pd.Series([np.nan for a in range(max_frames)])
    for i, prompt in prompts.items():
        prompt_series[i] = prompt
    # prompt_series = prompt_series.astype(str)
    prompt_series = prompt_series.ffill().bfill()
    return prompt_series

if key_frames:
    try:
        angle_series = get_inbetweens(parse_key_frames(angle))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `angle` correctly for key frames.\n"
            "Attempting to interpret `angle` as "
            f'"0: ({angle})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        angle = f"0: ({angle})"
        angle_series = get_inbetweens(parse_key_frames(angle))

    try:
        zoom_series = get_inbetweens(parse_key_frames(zoom))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `zoom` correctly for key frames.\n"
            "Attempting to interpret `zoom` as "
            f'"0: ({zoom})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        zoom = f"0: ({zoom})"
        zoom_series = get_inbetweens(parse_key_frames(zoom))

    try:
        translation_x_series = get_inbetweens(parse_key_frames(translation_x))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `translation_x` correctly for key frames.\n"
            "Attempting to interpret `translation_x` as "
            f'"0: ({translation_x})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        translation_x = f"0: ({translation_x})"
        translation_x_series = get_inbetweens(parse_key_frames(translation_x))

    try:
        translation_y_series = get_inbetweens(parse_key_frames(translation_y))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `translation_y` correctly for key frames.\n"
            "Attempting to interpret `translation_y` as "
            f'"0: ({translation_y})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        translation_y = f"0: ({translation_y})"
        translation_y_series = get_inbetweens(parse_key_frames(translation_y))

    try:
        translation_z_series = get_inbetweens(parse_key_frames(translation_z))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `translation_z` correctly for key frames.\n"
            "Attempting to interpret `translation_z` as "
            f'"0: ({translation_z})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        translation_z = f"0: ({translation_z})"
        translation_z_series = get_inbetweens(parse_key_frames(translation_z))

    try:
        rotation_3d_x_series = get_inbetweens(parse_key_frames(rotation_3d_x))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `rotation_3d_x` correctly for key frames.\n"
            "Attempting to interpret `rotation_3d_x` as "
            f'"0: ({rotation_3d_x})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        rotation_3d_x = f"0: ({rotation_3d_x})"
        rotation_3d_x_series = get_inbetweens(parse_key_frames(rotation_3d_x))

    try:
        rotation_3d_y_series = get_inbetweens(parse_key_frames(rotation_3d_y))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `rotation_3d_y` correctly for key frames.\n"
            "Attempting to interpret `rotation_3d_y` as "
            f'"0: ({rotation_3d_y})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        rotation_3d_y = f"0: ({rotation_3d_y})"
        rotation_3d_y_series = get_inbetweens(parse_key_frames(rotation_3d_y))

    try:
        rotation_3d_z_series = get_inbetweens(parse_key_frames(rotation_3d_z))
    except RuntimeError as e:
        print(
            "WARNING: You have selected to use key frames, but you have not "
            "formatted `rotation_3d_z` correctly for key frames.\n"
            "Attempting to interpret `rotation_3d_z` as "
            f'"0: ({rotation_3d_z})"\n'
            "Please read the instructions to find out how to use key frames "
            "correctly.\n"
        )
        rotation_3d_z = f"0: ({rotation_3d_z})"
        rotation_3d_z_series = get_inbetweens(parse_key_frames(rotation_3d_z))

else:
    angle = float(angle)
    zoom = float(zoom)
    translation_x = float(translation_x)
    translation_y = float(translation_y)
    translation_z = float(translation_z)
    rotation_3d_x = float(rotation_3d_x)
    rotation_3d_y = float(rotation_3d_y)
    rotation_3d_z = float(rotation_3d_z)

"""
### Extra Settings
 Partial Saves, Advanced Settings, Cutn Scheduling"""

#@markdown ####**Saving:**

intermediate_saves = 0#@param{type: 'raw'}
intermediates_in_subfolder = True #@param{type: 'boolean'}
#@markdown Intermediate steps will save a copy at your specified intervals. You can either format it as a single integer or a list of specific steps 

#@markdown A value of `2` will save a copy at 33% and 66%. 0 will save none.

#@markdown A value of `[5, 9, 34, 45]` will save at steps 5, 9, 34, and 45. (Make sure to include the brackets)


if type(intermediate_saves) is not list:
    if intermediate_saves:
        steps_per_checkpoint = math.floor((steps - skip_steps - 1) // (intermediate_saves+1))
        steps_per_checkpoint = steps_per_checkpoint if steps_per_checkpoint > 0 else 1
        print(f'Will save every {steps_per_checkpoint} steps')
    else:
        steps_per_checkpoint = steps+10
else:
    steps_per_checkpoint = None

#@markdown ---

#@markdown ####**Advanced Settings:**
#@markdown *There are a few extra advanced settings available if you double click this cell.*

#@markdown *Perlin init will replace your init, so uncheck if using one.*

perlin_init = False  #@param{type: 'boolean'}
perlin_mode = 'mixed' #@param ['mixed', 'color', 'gray']
set_seed = 'random_seed' #@param{type: 'string'}
eta = .75#@param{type: 'number'}
clamp_grad = True #@param{type: 'boolean'}
#@markdown clamp_max is a really important parameter for PAD v.3. I've tested it up to 1 ( see the README for an exhaustive comaprison chart of clamp_max vs '#pixelart" weight), but the best range will probably be closer to .075-2. This is likely to vary based on promt length and complexity. Details increase with clamp value size, but the more '#pixelart' in your prompt, the more it will be likely deep-fry at higher values with longer prompts. I reccomend experimenting! You *may* also want to reduce the number of steps by ~50-75 per increase of .05 clamp_max. If your images are coming out totally black, try lowering the clamp_max or adding more complexity to your prompt!
clamp_max =  .05#@param{type: 'number'}


### EXTRA ADVANCED SETTINGS:
randomize_class = True
clip_denoised = False
fuzzy_prompt = False
rand_mag = 0.05


#@markdown ---

#@markdown ####**Cutn Scheduling:**
#@markdown Format: `[40]*400+[20]*600` = 40 cuts for the first 400 /1000 steps, then 20 for the last 600/1000

#@markdown cut_overview and cut_innercut are cumulative for total cutn on any given step. Overview cuts see the entire image and are good for early structure, innercuts are your standard cutn.

cut_overview = "[14]*200+[12]*200+[4]*400+[0]*200" #@param {type: 'string'}       
cut_innercut ="[2]*200+[4]*200+[12]*400+[12]*200"#@param {type: 'string'}  

#@markdown For Pixel Art Diffusion, [cut_ic_pow](https://ezcharts.miraheze.org/wiki/Category:Cut_ic_pow) values seem to represent pixel density. Values between 1 and 100 all work. Values are expressed as a string; if you like the effect of a singe value acros the board, use the format ```[n]*1000```.

cut_ic_pow = "[100]*1000"#@param {type: 'string'}  
cut_icgray_p = "[0.7]*100+[0.6]*100+[0.45]*100+[0.3]*100+[0]*600"#@param {type: 'string'}

#@markdown ####**Transformation Settings:**
use_vertical_symmetry = False #@param {type:"boolean"}
use_horizontal_symmetry = False #@param {type:"boolean"}
transformation_percent = [0.09] #@param

"""### Prompts
`animation_mode: None` will only use the first set. `animation_mode: 2D / Video` will run through them per the set frames and hold on the last one.

------

#### Pixel Art Diffusion Prompts
Although all PAD models are fine-tuned on pixel art, PAD still may need gentle reminders that you *do* want pixel art. This is especially true when using the Hard or Soft PAD models (though possibly not with Secondary enabled). 

Lack of pixelation isn't really an issue with simple prompts in PAD 4k or PAD Expanded, but longer/more complex prompts may benefit from extra nudging. When needing a little extra pixellation, a good rule of thumb for prompt structuring is to follow a format like: 

```
["A cyberpunk city at sunset, #pixelart by van gogh","#pixelart"]
```
With a multi-part prompt, try playing around with how many times you remind it to generate images in a pixel art style. For examples of prmpt structure given varying length of prompts, see the README at the top of the page! 




"""

text_prompts = {
    0: ["Fire Queen spritesheet"],

    #100: ["This set of prompts start at frame 100","This prompt has weight five:5"],
}

image_prompts = {
    # 0:['ImagePromptsWorkButArentVeryGood.png:2',],
}

model = None
diffusion = None

def initModel():
    global model, diffusion
    print('Prepping model...')
    model, diffusion = create_model_and_diffusion(**model_config)
    model.load_state_dict(torch.load(f'{model_path}/{diffusion_model}.pt', map_location='cpu'))
    model.requires_grad_(False).eval().to(device)
    for name, param in model.named_parameters():
        if 'qkv' in name or 'norm' in name or 'proj' in name:
            param.requires_grad_()
    if model_config['use_fp16']:
        model.convert_to_fp16()

if __name__ == '__main__':
    initModel()
    res = do_run("unicorn magician spritesheet")
    print("res:", res)
