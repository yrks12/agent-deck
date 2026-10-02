#!/usr/bin/env python3
"""Generate the Agent Deck app icon: one of the agents, looking at you.

The characters the app draws for every desk (DeckUI/AvatarView.swift) are a
rounded, gradient-lit shape with two dark capsule eyes, a catch-light on each
eye, a soft highlight top left and, on some, a blush. The icon is one of them
-- in the berry pink/red of the `atlas` desk (Theme.avatarTints[11]) -- on
the app's own near-black canvas, so the Dock icon and the roster read as one
cast.

Every size is drawn from the geometry at that size (supersampled, then
downsampled), not shrunk from the 1024 master: at 16 and 32 px the character
grows, the eyes grow and the blush and highlights are dropped, so the face
still reads as a face in Finder's list view.

Outputs:
  macos/Resources/AppIcon.png                      1024 master (Mac squircle)
  macos/Resources/AppIcon.iconset/icon_*.png       every Mac size, hand-tuned
  macos/App/Assets.xcassets/AppIcon.appiconset     the Xcode projects' set
  ios/AgentDeckPhone/Assets.xcassets/AppIcon...    iOS light, dark, tinted

Run: `python3 macos/Scripts/generate-app-icon.py`   (needs Pillow)
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
MACOS = HERE.parent
REPO = MACOS.parent
MASTER = MACOS / "Resources" / "AppIcon.png"
ICONSET = MACOS / "Resources" / "AppIcon.iconset"
XC_SET = MACOS / "App" / "Assets.xcassets" / "AppIcon.appiconset"
IOS_SET = REPO / "ios" / "AgentDeckPhone" / "Assets.xcassets" / "AppIcon.appiconset"

# Theme.avatarTints[11], "berry" -- the atlas desk.
BERRY = (219, 66, 92)
INK = (20, 18, 33)          # AvatarEyes.ink
BLUSH = (255, 115, 128)
CANVAS_TOP = (38, 36, 46)
CANVAS_BOTTOM = (10, 10, 14)


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def superellipse(box, exponent=5.0, steps=720):
    x0, y0, x1, y1 = box
    cx, cy, a, b = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + math.copysign(abs(c) ** (2 / exponent), c) * a,
                    cy + math.copysign(abs(s) ** (2 / exponent), s) * b))
    return pts


def gradient(size, top, bottom, y0=0, y1=None):
    y1 = size if y1 is None else y1
    img = Image.new("RGB", (1, size))
    for y in range(size):
        t = min(max((y - y0) / max(y1 - y0, 1), 0), 1)
        img.putpixel((0, y), mix(top, bottom, t))
    return img.resize((size, size)).convert("RGBA")


def draw_icon(px: int, *, tile: str = "mac", background: bool = True,
              ss: int | None = None) -> Image.Image:
    """The icon at `px` pixels.

    tile: "mac"  -- Big Sur grid: a squircle with a margin and a drop shadow.
          "full" -- full-bleed square (iOS masks it itself).
    background: False draws the character alone on transparency (iOS dark).
    """
    small = px <= 32
    ss = ss or max(4, 1024 // px)
    S = px * ss
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ── the tile ────────────────────────────────────────────────────────────
    if tile == "mac":
        margin = S * (0.06 if small else 0.098)
        box = (margin, margin * (0.9 if small else 0.85), S - margin,
               S - margin * (1.1 if small else 1.15))
        tile_mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(tile_mask).polygon(superellipse(box), fill=255)
        if background and not small:
            shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            shadow.putalpha(tile_mask.point(lambda v: v * 90 // 255))
            shadow = shadow.transform(shadow.size, Image.AFFINE, (1, 0, 0, 0, 1, -S * 0.012))
            shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.018))
            out.alpha_composite(shadow)
        tx0, ty0, tx1, ty1 = box
    else:
        tile_mask = Image.new("L", (S, S), 255)
        tx0, ty0, tx1, ty1 = 0, 0, S, S
    tw, th = tx1 - tx0, ty1 - ty0

    if background:
        bg = gradient(S, CANVAS_TOP, CANVAS_BOTTOM, int(ty0), int(ty1))
        out.paste(bg, (0, 0), tile_mask)
        if not small:
            # a faint pink glow behind the character: it is lit, not pasted on
            glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            r = tw * 0.42
            cx, cy = (tx0 + tx1) / 2, ty0 + th * 0.55
            ImageDraw.Draw(glow).ellipse([cx - r, cy - r * 0.8, cx + r, cy + r * 0.8],
                                         fill=BERRY + (70,))
            glow = glow.filter(ImageFilter.GaussianBlur(S * 0.07))
            clipped = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            clipped.paste(glow, (0, 0), tile_mask)
            out.alpha_composite(clipped)

    # ── the character: a pebble-squircle, like the roster's ─────────────────
    scale = 0.80 if small else (0.66 if tile == "mac" else 0.64)
    cw = tw * scale
    ch = cw * (0.92 if small else 0.88)
    cx = (tx0 + tx1) / 2
    cy = ty0 + th * (0.54 if small else 0.55)
    body_box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
    body_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(body_mask).polygon(superellipse(body_box, exponent=3.2), fill=255)

    if not small and background:
        drop = Image.new("RGBA", (S, S), BERRY + (0,))
        drop.putalpha(body_mask.point(lambda v: v * 110 // 255))
        drop = drop.transform(drop.size, Image.AFFINE, (1, 0, 0, 0, 1, -ch * 0.05))
        drop = drop.filter(ImageFilter.GaussianBlur(cw * 0.06))
        out.alpha_composite(drop)

    body = gradient(S, mix(BERRY, (255, 255, 255), 0.24), mix(BERRY, (0, 0, 0), 0.16),
                    int(body_box[1]), int(body_box[3]))
    out.paste(body, (0, 0), body_mask)

    if not small:
        # catch-light, top left, clipped to the body (AvatarView.body(of:))
        hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        hw, hh = cw * 0.52, ch * 0.24
        hx, hy = cx - cw * 0.16, cy - ch * 0.30
        ImageDraw.Draw(hl).ellipse([hx - hw / 2, hy - hh / 2, hx + hw / 2, hy + hh / 2],
                                   fill=(255, 255, 255, 46))
        hl = hl.rotate(24, center=(hx, hy), resample=Image.BICUBIC)
        hl = hl.filter(ImageFilter.GaussianBlur(cw * 0.012))
        clip = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        clip.paste(hl, (0, 0), body_mask)
        out.alpha_composite(clip)

    d = ImageDraw.Draw(out)
    # ── blush ───────────────────────────────────────────────────────────────
    spread = 0.22 if small else 0.19
    eye_y = cy - ch * (0.08 if small else 0.05)
    if not small:
        for side in (-1, 1):
            bx = cx + side * cw * (spread + 0.14)
            by = eye_y + ch * 0.22
            bw, bh = cw * 0.17, ch * 0.10
            blush = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            ImageDraw.Draw(blush).rounded_rectangle(
                [bx - bw / 2, by - bh / 2, bx + bw / 2, by + bh / 2], radius=bh / 2,
                fill=BLUSH + (150,))
            blush = blush.filter(ImageFilter.GaussianBlur(cw * 0.01))
            out.alpha_composite(blush)

    # ── the eyes: tall capsules, looking slightly up-right at you ───────────
    ew = cw * (0.17 if small else 0.135)
    eh = cw * (0.27 if small else 0.235)
    gaze_x = 0 if small else cw * 0.012
    gaze_y = 0 if small else -ch * 0.012
    for side in (-1, 1):
        ex = cx + side * cw * spread + gaze_x
        ey = eye_y + gaze_y
        d.rounded_rectangle([ex - ew / 2, ey - eh / 2, ex + ew / 2, ey + eh / 2],
                            radius=ew / 2, fill=INK + (255,))
        if not small:
            r = ew * 0.19
            gx, gy = ex + ew * 0.10, ey - eh * 0.20
            d.ellipse([gx - r, gy - r, gx + r, gy + r], fill=(255, 255, 255, 235))

    # ── a small smile: the mouth the characters open when they talk (the call
    # stage), closed into a grin. It is what makes two dark ovals a face and
    # not a snout at 16 px.
    mw = cw * (0.30 if small else 0.20)
    mt = cw * (0.075 if small else 0.042)
    my = eye_y + eh * 0.5 + ch * (0.10 if small else 0.11)
    d.arc([cx - mw / 2, my - mw * 0.45, cx + mw / 2, my + mw * 0.30],
          start=20, end=160, fill=INK + (255,), width=int(mt))

    return out.resize((px, px), Image.LANCZOS)


MAC_SIZES = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
             (256, 1), (256, 2), (512, 1), (512, 2)]


def mac(px: int) -> Image.Image:
    """The Mac's icon is full bleed and opaque. macOS 26 masks every app icon
    to its own squircle; one drawn on the Big Sur grid (transparent margins)
    is shrunk onto a grey plate instead, which read as the wrong icon. And
    RGB, not RGBA: measured, an all-opaque RGBA PNG is still plated; only an
    image with no alpha channel is masked."""
    return draw_icon(px, tile="full").convert("RGB")


def write_mac() -> None:
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    mac(1024).save(MASTER)
    ICONSET.mkdir(parents=True, exist_ok=True)
    rendered: dict[int, Image.Image] = {}
    for pt, scale in MAC_SIZES:
        px = pt * scale
        rendered.setdefault(px, mac(px))
        name = f"icon_{pt}x{pt}" + ("@2x" if scale == 2 else "") + ".png"
        rendered[px].save(ICONSET / name)
    # The Xcode asset catalog names its files by pixel size.
    XC_SET.mkdir(parents=True, exist_ok=True)
    for px in (16, 32, 64, 128, 256, 512, 1024):
        rendered.setdefault(px, mac(px)).save(XC_SET / f"icon_{px}.png")
    print(f"wrote {MASTER}, {ICONSET}, {XC_SET}")


def write_ios() -> None:
    IOS_SET.mkdir(parents=True, exist_ok=True)
    # Light: opaque, full bleed -- iOS rejects an icon with an alpha channel.
    draw_icon(1024, tile="full").convert("RGB").save(IOS_SET / "AppIcon.png")
    # Dark: the character alone; the system draws the dark backdrop.
    dark = draw_icon(1024, tile="full", background=False)
    dark.save(IOS_SET / "AppIcon-dark.png")
    # Tinted: a greyscale of the character on black, which the system tints.
    tinted = Image.new("RGBA", (1024, 1024), (0, 0, 0, 255))
    tinted.alpha_composite(dark)
    # Brightened: the system tints the light parts, so the body should be near
    # white and the eyes and smile stay dark.
    grey = tinted.convert("L").point(lambda v: min(255, int(v * 1.7)))
    grey.convert("RGB").save(IOS_SET / "AppIcon-tinted.png")
    print(f"wrote {IOS_SET}")


def main() -> None:
    write_mac()
    write_ios()


if __name__ == "__main__":
    main()
