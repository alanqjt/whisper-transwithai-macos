#!/usr/bin/env python3
"""生成 app 图标主图 (1024x1024 PNG): 蓝紫渐变圆角底 + 白色大写 T + 黄色字幕条。"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

S = 1024
RADIUS = 230
TOP = (0x3B, 0x82, 0xF6)      # 蓝
BOT = (0x7C, 0x3A, 0xED)      # 紫
YELLOW = (0xFC, 0xD3, 0x4D)
FONTS = [                                                  # 粗体拉丁字体, 取第一个存在的
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/icon_1024.png"

# 垂直渐变
grad = Image.new("RGB", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    c = tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3))
    gd.line([(0, y), (S, y)], fill=c)

# 圆角遮罩
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], RADIUS, fill=255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)

# 白色大写 T
fp = next((f for f in FONTS if os.path.exists(f)), FONTS[-1])
font = ImageFont.truetype(fp, 620, index=0)
d.text((S // 2, 430), "T", font=font, fill=(255, 255, 255, 255), anchor="mm")

# 两条黄色字幕条
def bar(w, cy):
    x0 = (S - w) // 2
    d.rounded_rectangle([x0, cy, x0 + w, cy + 64], radius=32, fill=YELLOW)

bar(560, 770)
bar(360, 860)

img.save(OUT)
print("已生成", OUT)
