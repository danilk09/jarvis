"""Draws the Jarvis icon (the dashboard's orb) → desktop/icon.ico, used by the desktop app
window and the shortcut. Run again after changing the colors."""

import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 256
GLOW = (64, 255, 190)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse((8, 8, S - 8, S - 8), fill=(2, 8, 6, 255))                          # dark disc
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse((40, 40, S - 40, S - 40), outline=GLOW + (255,), width=22)
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))               # soft halo
d.ellipse((44, 44, S - 44, S - 44), outline=GLOW + (255,), width=12)         # ring
d.ellipse((96, 96, S - 96, S - 96), fill=GLOW + (255,))                      # core
d.ellipse((8, 8, S - 8, S - 8), outline=(40, 90, 70, 255), width=6)          # rim

img.save(os.path.join(ROOT, "desktop", "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("desktop/icon.ico written")
