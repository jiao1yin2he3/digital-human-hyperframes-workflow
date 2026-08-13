#!/usr/bin/env python3
"""生成加强版数字人投影层 PNG（3:4 288x375，阴影更明显）

用法:
  python3 scripts/gen_proj_strong.py [--output <path>] [--preview-dir <dir>]

默认输出 /tmp/proj6/proj_layer_strong.png（兼容旧行为）；
pipeline_daily.py 通过 --output 指定项目目录下的 shadow_layer.png。
"""
from PIL import Image, ImageDraw, ImageFilter, ImageChops
import argparse
import os

def main():
    parser = argparse.ArgumentParser(description="生成数字人投影层 PNG")
    parser.add_argument("--output", default="/tmp/proj6/proj_layer_strong.png",
                        help="输出 PNG 路径（默认 /tmp/proj6/proj_layer_strong.png）")
    parser.add_argument("--preview-dir", default="/tmp/proj6",
                        help="预览图输出目录（默认 /tmp/proj6）")
    args = parser.parse_args()

    inner_w, inner_h = 260, 347
    radius = 16
    pad = 14
    W, H = inner_w + pad*2, inner_h + pad*2  # 284x371

    shape = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shape).rounded_rectangle(
        [pad, pad, W-pad-1, H-pad-1], radius=radius, fill=255)

    # 加强：偏移更大(0, 12)，模糊更大(16)，alpha 75%
    proj = Image.new("L", (W, H), 0)
    proj.paste(shape, (0, 12))
    proj = proj.filter(ImageFilter.GaussianBlur(16))
    proj = proj.point(lambda v: int(v * 0.75))

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hole = shape.point(lambda v: 255 - v)
    final_alpha = ImageChops.multiply(proj, hole)
    layer.putalpha(final_alpha)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    layer.save(args.output)
    print(f"加强投影层: {args.output} {W}x{H}")

    # 浅色背景预览（验证阴影可见）
    os.makedirs(args.preview_dir, exist_ok=True)
    preview_light = os.path.join(args.preview_dir, "preview_light.png")
    bg = Image.new("RGB", (W+60, H+60), (240, 244, 248))
    bg.paste(layer, (30, 30), layer)
    bg.save(preview_light)
    # 深色背景预览（模拟视频背景）
    preview_dark = os.path.join(args.preview_dir, "preview_dark.png")
    bg2 = Image.new("RGB", (W+60, H+60), (16, 28, 58))
    bg2.paste(layer, (30, 30), layer)
    bg2.save(preview_dark)
    print(f"预览: {preview_light}, {preview_dark}")

if __name__ == "__main__":
    main()
