# -*- coding: utf-8 -*-
import cv2
from PIL import Image
import numpy as np
import importlib
import os
import argparse
from tqdm import tqdm
import matplotlib.pyplot as plt
from matplotlib import animation
import torch

from core.utils import to_tensors

parser = argparse.ArgumentParser(description="E2FGVI")
parser.add_argument("-v", "--video", type=str, required=True)
parser.add_argument("-c", "--ckpt", type=str, required=True)
parser.add_argument("-m", "--mask", type=str, required=True)
parser.add_argument("--model", type=str, choices=['e2fgvi', 'e2fgvi_hq'])
parser.add_argument("--step", type=int, default=10)
parser.add_argument("--num_ref", type=int, default=-1)
parser.add_argument("--neighbor_stride", type=int, default=10)
parser.add_argument("--savefps", type=int, default=24)

# frame_stride must be evenly divisible by neighbor_stride
parser.add_argument("--frame_stride", type=int, default=40)


# args for e2fgvi_hq (which can handle videos with arbitrary resolution)
parser.add_argument("--set_size", action='store_true', default=False)
parser.add_argument("--width", type=int)
parser.add_argument("--height", type=int)

args = parser.parse_args()

ref_length = args.step  # ref_step
num_ref = args.num_ref
neighbor_stride = args.neighbor_stride
default_fps = args.savefps


# sample reference frames from the whole video
def get_ref_index(f, neighbor_ids, length):
    ref_index = []
    if num_ref == -1:
        for i in range(0, length, ref_length):
            if i not in neighbor_ids:
                ref_index.append(i)
    else:
        start_idx = max(0, f - ref_length * (num_ref // 2))
        end_idx = min(length, f + ref_length * (num_ref // 2))
        for i in range(start_idx, end_idx + 1, ref_length):
            if i not in neighbor_ids:
                if len(ref_index) > num_ref:
                    break
                ref_index.append(i)
    return ref_index


# read frame-wise masks
def read_mask(mpath, size):
    masks = []
    mnames = os.listdir(mpath)
    mnames.sort()
    for mp in mnames:
        m = Image.open(os.path.join(mpath, mp))
        m = m.resize(size, Image.NEAREST)
        m = np.array(m.convert('L'))
        m = np.array(m > 0).astype(np.uint8)
        m = cv2.dilate(m,
                       cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3)),
                       iterations=4)
        masks.append(Image.fromarray(m * 255))
    return masks


#  read frames from video
def read_frame_from_videos(args):
    vname = args.video
    frames = []
    if args.use_mp4:
        vidcap = cv2.VideoCapture(vname)
        success, image = vidcap.read()
        count = 0
        while success:
            image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            frames.append(image)
            success, image = vidcap.read()
            count += 1
    else:
        lst = os.listdir(vname)
        lst.sort()
        fr_lst = [vname + '/' + name for name in lst]
        for fr in fr_lst:
            image = cv2.imread(fr)
            image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            frames.append(image)
    return frames


# resize frames
def resize_frames(frames, size=None):
    if size is not None:
        frames = [f.resize(size) for f in frames]
    else:
        size = frames[0].size
    return frames, size


def main_worker():
    # set up models
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if args.model == "e2fgvi":
        size = (432, 240)
    elif args.set_size:
        size = (args.width, args.height)
    else:
        size = None

    net = importlib.import_module('model.' + args.model)
    model = net.InpaintGenerator().half().to(device)
    data = torch.load(args.ckpt, map_location=device)
    model.load_state_dict(data)
    print(f'Loading model from: {args.ckpt}')
    model.eval()

    # prepare datset
    args.use_mp4 = True if args.video.endswith('.mp4') else False
    print(
        f'Loading videos and masks from: {args.video} | INPUT MP4 format: {args.use_mp4}'
    )
    rframes = read_frame_from_videos(args)
    rframes, size = resize_frames(rframes, size)
    h, w = size[1], size[0]
    video_length = len(rframes)
    rmasks = read_mask(args.mask, size)

    comp_frames = [None] * video_length

    framestride = args.frame_stride

    x_frames = [rframes[i:i + framestride] for i in range(0, len(rframes), framestride)]
    x_masks = [rmasks[i:i + framestride] for i in range(0, len(rmasks), framestride)]
    print(f'Start test...')
    for itern in range(0, len(x_frames), 1):
        stride_length = len(x_frames[itern])

        strides = len(x_frames)

        #print(strides)
        #print(stride_length)

        loopstartframe = 0
        loopendframe = stride_length

        xfram = x_frames[itern]
        xmask = x_masks[itern]

        if (itern < strides - 1):
            for xframappend in range(0, neighbor_stride):
                xfram.append(x_frames[itern + 1][xframappend])
                xmask.append(x_masks[itern + 1][xframappend])

        # if (itern > 0):
        #     for xframappend in range(1, neighbor_stride + 1):
        #         xfram.insert(0, x_frames[itern - 1][len(x_frames[itern - 1]) - xframappend])
        #         xmask.insert(0, x_masks[itern - 1][len(x_masks[itern - 1]) - xframappend])

        imgs = to_tensors()(xfram).unsqueeze(0) * 2 - 1
        frames = [np.array(f).astype(np.uint8) for f in xfram]

        binary_masks = [
            np.expand_dims((np.array(m) != 0).astype(np.uint8), 2) for m in xmask
        ]
        masks = to_tensors()(xmask).unsqueeze(0)
        imgs, masks = imgs.half().to(device), masks.half().to(device)

        if (itern > 0):
            loopstartframe = neighbor_stride
        else:
            loopstartframe = 0

        if (itern < strides - 1):
            loopendframe = stride_length + neighbor_stride
        else:
            loopendframe = stride_length

        # completing holes by e2fgvi

        for f in tqdm(range(loopstartframe, loopendframe, neighbor_stride)):
            #print(f)
            #print(f'meh {max(loopstartframe, f - neighbor_stride)} muh {min(loopendframe, f + neighbor_stride + 1)}')
            neighbor_ids = [
                i for i in range(max(loopstartframe, f - neighbor_stride),
                                 min(loopendframe, f + neighbor_stride + 1))
            ]
            #print(neighbor_ids)
            # The frame +- 5 frames before or after.  Beginning: zero frames before.   End: 0 frames after.

            ref_ids = get_ref_index(f, neighbor_ids, loopendframe)
            selected_imgs = imgs[:1, neighbor_ids + ref_ids, :, :, :]
            selected_masks = masks[:1, neighbor_ids + ref_ids, :, :, :]

            with torch.no_grad():
                masked_imgs = selected_imgs * (1 - selected_masks)
                mod_size_h = 60
                mod_size_w = 108
                h_pad = (mod_size_h - h % mod_size_h) % mod_size_h
                w_pad = (mod_size_w - w % mod_size_w) % mod_size_w
                masked_imgs = torch.cat(
                    [masked_imgs, torch.flip(masked_imgs, [3])],
                    3)[:, :, :, :h + h_pad, :]
                masked_imgs = torch.cat(
                    [masked_imgs, torch.flip(masked_imgs, [4])],
                    4)[:, :, :, :, :w + w_pad].half()
                pred_imgs, _ = model(masked_imgs, len(neighbor_ids))
                pred_imgs = pred_imgs[:, :, :h, :w]
                pred_imgs = (pred_imgs + 1) / 2
                pred_imgs = pred_imgs.cpu().permute(0, 2, 3, 1).numpy() * 255
                for i in range(0, len(neighbor_ids)):
                    idx = neighbor_ids[i]
                    img = np.array(pred_imgs[i]).astype(
                        np.uint8) * binary_masks[idx] + frames[idx] * (
                                  1 - binary_masks[idx])
                    if comp_frames[(itern * framestride) + idx] is None:
                        comp_frames[(itern * framestride) + idx] = img
                    else:
                        comp_frames[(itern * framestride) + idx] = comp_frames[(itern * framestride) + idx].astype(
                            np.float32) * 0.5 + img.astype(np.float32) * 0.5

    # saving videos
    print('Saving videos...')
    save_dir_name = 'results'
    ext_name = '_results.mp4'
    save_base_name = args.video.split('/')[-1]
    save_name = save_base_name.replace(
        '.mp4', ext_name) if args.use_mp4 else save_base_name + ext_name
    if not os.path.exists(save_dir_name):
        os.makedirs(save_dir_name)
    save_path = os.path.join(save_dir_name, save_name)
    writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*"mp4v"),
                             default_fps, size)
    for f in range(video_length):
        comp = comp_frames[f].astype(np.uint8)
        writer.write(cv2.cvtColor(comp, cv2.COLOR_BGR2RGB))
    writer.release()
    print(f'Finish test! The result video is saved in: {save_path}.')

    # show results
    print('Let us enjoy the result!')
    fig = plt.figure('Let us enjoy the result')
    ax1 = fig.add_subplot(1, 2, 1)
    ax1.axis('off')
    ax1.set_title('Original Video')
    ax2 = fig.add_subplot(1, 2, 2)
    ax2.axis('off')
    ax2.set_title('Our Result')
    imdata1 = ax1.imshow(frames[0])
    imdata2 = ax2.imshow(comp_frames[0].astype(np.uint8))

    def update(idx):
        imdata1.set_data(rframes[idx])
        imdata2.set_data(comp_frames[idx].astype(np.uint8))

    fig.tight_layout()
    anim = animation.FuncAnimation(fig,
                                   update,
                                   frames=len(comp_frames),
                                   interval=50)
    plt.show()


if __name__ == '__main__':
    main_worker()
