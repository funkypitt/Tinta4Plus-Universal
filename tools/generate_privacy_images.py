#!/usr/bin/env python3
"""
Generate the geometric/abstract privacy images shown on the eInk panel
when it is switched off (eink-disable<N>.jpg, 2560x1600).

Colour themes are borrowed from the Plume keyboard presets
(github.com/funkypitt/plume-keyboard, uix/theme/presets). Everything is
drawn with Pillow only, 2x supersampled and downscaled for clean edges;
seeds are fixed so the output is reproducible.

    python3 tools/generate_privacy_images.py          # writes eink-disable5..16.jpg
    python3 tools/generate_privacy_images.py --out /tmp/x --sheet  # plus a contact sheet
"""
import argparse
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter

W, H = 2560, 1600          # eInk panel
SS = 2                     # supersampling factor
FIRST_INDEX = 5            # eink-disable1..3 are the text images, 4 is personal


def hx(c):
    c = c.lstrip('#')
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ----------------------------------------------------------------------
# Palettes (Plume presets: surface / primary / secondary / tertiary ...)
# ----------------------------------------------------------------------
THEMES = {
    'plume-light': dict(bg='#D1D3D9', c=['#FFFFFF', '#ABB1BA', '#007AFF', '#D6E6FF', '#34C759', '#C2C4CA']),
    'plume-dark': dict(bg='#2C2C2C', c=['#0A84FF', '#3A3A3C', '#6B6B6B', '#00397A', '#30D158', '#232323']),
    'catppuccin-mocha': dict(bg='#11111B', c=['#F4B8E4', '#F5C2E7', '#F2CDCD', '#CBA6F7', '#F38BA8', '#313244', '#45475A']),
    'emerald': dict(bg='#091208', c=['#67FB59', '#112E10', '#162D13', '#24521E', '#52F5F5', '#005252', '#0D1A0B']),
    'sunflower': dict(bg='#FFF9ED', c=['#F8E286', '#EDDCBB', '#6D5E0F', '#FFEDAB', '#314291', '#A4AFDE', '#E5D7BA']),
    'deep-sea-light': dict(bg='#DBE3FF', c=['#314BA1', '#BBCAFA', '#243469', '#99B1FF', '#E8EDFF', '#F2C7AC']),
    'deep-sea-dark': dict(bg='#0A0B17', c=['#BEC2FF', '#131636', '#AFBEF1', '#1B1D42', '#FFB08C', '#0F1026']),
    'snowfall': dict(bg='#F8F8F8', c=['#1D2023', '#BAC5DB', '#E5E5E5', '#819EDE', '#303133', '#CCCCCC']),
    'steel-grey': dict(bg='#2E2E2E', c=['#BBD5F0', '#393E47', '#F0F8FF', '#94B6FF', '#454545', '#585B5E']),
    'cotton-candy': dict(bg='#FFEDF9', c=['#FFA6F5', '#B02378', '#FFBAE9', '#611958', '#005422', '#F29DE3']),
    'amoled-purple': dict(bg='#000000', c=['#D0BCFF', '#3A2966', '#CFBAFF', '#F1FFA3', '#31264F', '#1E192B']),
    'high-contrast-yellow': dict(bg='#000000', c=['#FFFF00', '#FFFFFF', '#858585', '#B9B9B9']),
    'gradient-aurora': dict(bg='#0E0E1A', c=['#F786F1', '#102347', '#F7D0F5', '#7FEB86', '#0F2F6E', '#7D56BF']),
}


def canvas(theme):
    img = Image.new('RGB', (W * SS, H * SS), hx(theme['bg']))
    return img, ImageDraw.Draw(img), [hx(c) for c in theme['c']]


def finish(img):
    return img.resize((W, H), Image.LANCZOS)


def vertical_gradient(size, top, bottom):
    w, h = size
    g = Image.new('RGB', (1, h))
    px = g.load()
    for y in range(h):
        px[0, y] = mix(top, bottom, y / max(1, h - 1))
    return g.resize((w, h))


# ----------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------
def feather_arcs(theme, seed=1):
    """Plume: concentric quarter arcs fanning from the bottom-left corner."""
    img, d, c = canvas(theme)
    rnd = random.Random(seed)
    cx, cy = -W * SS * 0.05, H * SS * 1.1
    rmax = math.hypot(W * SS - cx, cy) * 1.02
    n = 22
    for i in range(n, 0, -1):
        r = rmax * i / n
        color = c[i % 2] if i % 7 else c[2]
        if i == 5:
            color = c[2]
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    # a few accent dots along one arc
    r = rmax * 9.5 / n
    for k in range(7):
        a = math.radians(-78 + k * 11)
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        d.ellipse([x - 22 * SS, y - 22 * SS, x + 22 * SS, y + 22 * SS], fill=c[4] if k == 3 else c[3])
    return finish(img)


def diagonal_bands(theme, seed=2):
    """Plume dark: wide diagonal bands with one accent band and a dot."""
    img, d, c = canvas(theme)
    rnd = random.Random(seed)
    band = 150 * SS
    for i in range(-20, 40):
        x0 = i * band
        color = [c[1], c[5], c[2], c[1]][i % 4]
        if i == 7:
            color = c[0]
        if i == 12:
            color = c[3]
        d.polygon([(x0, 0), (x0 + band, 0), (x0 + band - H * SS, H * SS), (x0 - H * SS, H * SS)], fill=color)
    d.ellipse([W * SS * 0.70, H * SS * 0.20, W * SS * 0.70 + 260 * SS, H * SS * 0.20 + 260 * SS], fill=c[4])
    return finish(img)


def rounded_tiles(theme, seed=3):
    """Catppuccin: a loose grid of rounded squares in pastel accents."""
    img, d, c = canvas(theme)
    rnd = random.Random(seed)
    cell = 160 * SS
    for gy in range(0, H * SS + cell, cell):
        for gx in range(0, W * SS + cell, cell):
            if rnd.random() < 0.42:
                continue
            s = rnd.choice([0.35, 0.5, 0.5, 0.7, 0.9]) * cell
            ox, oy = gx + (cell - s) / 2, gy + (cell - s) / 2
            color = rnd.choice(c[:5]) if rnd.random() < 0.45 else rnd.choice(c[5:])
            d.rounded_rectangle([ox, oy, ox + s, oy + s], radius=s * 0.28, fill=color)
    return finish(img)


def hexagons(theme, seed=4):
    """Emerald: hexagon tessellation, greens with sparse cyan."""
    img, d, c = canvas(theme)
    rnd = random.Random(seed)
    r = 95 * SS
    dx, dy = r * math.sqrt(3), r * 1.5
    row = 0
    y = -r
    while y < H * SS + r:
        x = -r + (dx / 2 if row % 2 else 0)
        while x < W * SS + r:
            t = rnd.random()
            color = c[6] if t < 0.45 else c[1] if t < 0.7 else c[2] if t < 0.88 else c[3] if t < 0.96 else (c[0] if t < 0.985 else c[4])
            pts = [(x + (r - 4 * SS) * math.cos(math.radians(60 * k + 30)),
                    y + (r - 4 * SS) * math.sin(math.radians(60 * k + 30))) for k in range(6)]
            d.polygon(pts, fill=color)
            x += dx
        y += dy
        row += 1
    return finish(img)


def sun_rays(theme, seed=5):
    """Sunflower: rays from an off-centre sun, cream and gold, one blue disc."""
    img, d, c = canvas(theme)
    cx, cy = W * SS * 0.72, H * SS * 0.38
    n = 36
    R = math.hypot(W * SS, H * SS) * 1.2
    for i in range(n):
        a0, a1 = math.radians(i * 360 / n), math.radians((i + 1) * 360 / n)
        color = [c[0], c[1], c[3], c[6]][i % 4]
        d.polygon([(cx, cy), (cx + R * math.cos(a0), cy + R * math.sin(a0)),
                   (cx + R * math.cos(a1), cy + R * math.sin(a1))], fill=color)
    for k, rr in enumerate([420, 330, 240]):
        rr *= SS
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=[c[2], c[0], c[2]][k])
    rr = 110 * SS
    px, py = W * SS * 0.18, H * SS * 0.72
    d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=c[4])
    d.ellipse([px - rr * 0.55, py - rr * 0.55, px + rr * 0.55, py + rr * 0.55], fill=c[5])
    return finish(img)


def waves(theme, seed=6, layers=9):
    """Deep Sea: stacked sine waves."""
    img, d, c = canvas(theme)
    rnd = random.Random(seed)
    for i in range(layers):
        base = H * SS * (0.22 + 0.09 * i)
        amp = (40 + 18 * i) * SS
        freq = 1.2 + 0.35 * i
        phase = rnd.uniform(0, 6.28)
        pts = [(x, base + amp * math.sin(freq * 2 * math.pi * x / (W * SS) + phase))
               for x in range(0, W * SS + 20 * SS, 20 * SS)]
        pts += [(W * SS, H * SS), (0, H * SS)]
        color = c[(i + 1) % len(c)] if i != layers - 2 else c[0]
        d.polygon(pts, fill=color)
    return finish(img)


def halftone(theme, seed=8):
    """Snowfall: a dot grid whose radius follows a soft diagonal gradient."""
    img, d, c = canvas(theme)
    cell = 70 * SS
    for gy in range(cell // 2, H * SS, cell):
        for gx in range(cell // 2, W * SS, cell):
            t = (gx / (W * SS) * 0.6 + gy / (H * SS) * 0.4)
            r = cell * 0.08 + cell * 0.36 * (1 - t) ** 1.6
            color = c[0] if t < 0.5 else c[4]
            if (gx // cell + gy // cell) % 9 == 0:
                color = c[3]
            d.ellipse([gx - r, gy - r, gx + r, gy + r], fill=color)
    return finish(img)


def bauhaus(theme, seed=9):
    """Steel Grey: overlapping circles and half-discs on a square grid."""
    img, d, c = canvas(theme)
    rnd = random.Random(seed)
    cell = 320 * SS
    for gy in range(0, H * SS, cell):
        for gx in range(0, W * SS, cell):
            k = rnd.random()
            fill = rnd.choice(c[1:2] + c[4:6])
            d.rectangle([gx, gy, gx + cell, gy + cell], fill=fill)
            acc = rnd.choice([c[0], c[2], c[3], c[4], c[5]])
            if k < 0.35:
                d.pieslice([gx, gy, gx + 2 * cell, gy + 2 * cell], 180, 270, fill=acc)
            elif k < 0.6:
                d.ellipse([gx + cell * 0.15, gy + cell * 0.15, gx + cell * 0.85, gy + cell * 0.85], fill=acc)
            elif k < 0.8:
                d.pieslice([gx - cell, gy, gx + cell, gy + 2 * cell], 270, 360, fill=acc)
    return finish(img)


def soft_blobs(theme, seed=10):
    """Cotton Candy: blurred overlapping ellipses."""
    img, d, c = canvas(theme)
    rnd = random.Random(seed)
    for i in range(14):
        rx, ry = rnd.uniform(260, 620) * SS, rnd.uniform(220, 520) * SS
        x, y = rnd.uniform(0, W * SS), rnd.uniform(0, H * SS)
        d.ellipse([x - rx, y - ry, x + rx, y + ry], fill=rnd.choice(c))
    img = img.filter(ImageFilter.GaussianBlur(70 * SS))
    d = ImageDraw.Draw(img)
    for i in range(5):  # a few crisp rings on top
        r = rnd.uniform(90, 200) * SS
        x, y = rnd.uniform(0.1, 0.9) * W * SS, rnd.uniform(0.1, 0.9) * H * SS
        d.ellipse([x - r, y - r, x + r, y + r], outline=c[1] if i % 2 else c[3], width=14 * SS)
    return finish(img)


def neon_grid(theme, seed=11):
    """AMOLED purple: perspective floor grid with a lime horizon dot."""
    img, d, c = canvas(theme)
    horizon = H * SS * 0.42
    vx = W * SS * 0.5
    for i in range(-30, 31):
        x = vx + i * 140 * SS
        d.line([(vx + (x - vx) * 0.02, horizon), (x * 1.8 - vx * 0.8, H * SS)], fill=c[1], width=4 * SS)
    y = horizon
    step = 6 * SS
    while y < H * SS:
        d.line([(0, y), (W * SS, y)], fill=c[4] if int(y) % 3 else c[1], width=4 * SS)
        step *= 1.22
        y += step
    d.line([(0, horizon), (W * SS, horizon)], fill=c[0], width=6 * SS)
    r = 90 * SS
    d.ellipse([vx - r, horizon - 2 * r - 20 * SS, vx + r, horizon - 20 * SS], fill=c[3])
    for k in range(1, 4):  # faint upper arcs
        rr = (260 + 180 * k) * SS
        d.arc([vx - rr, horizon - rr, vx + rr, horizon + rr], 200, 340, fill=c[2], width=5 * SS)
    return finish(img)


def chevrons(theme, seed=12):
    """High contrast: bold yellow/black chevrons."""
    img, d, c = canvas(theme)
    hgt = 180 * SS
    peak = 220 * SS
    for i in range(-2, 12):
        y = i * hgt
        color = c[0] if i % 2 else hx(theme['bg'])
        if i == 6:
            color = c[1]
        pts = []
        x = -W * SS * 0.1
        while x <= W * SS * 1.1:
            pts.append((x, y + (0 if (x // (W * SS / 4)) % 2 == 0 else peak)))
            x += W * SS / 4
        pts_up = [(x0, y0) for x0, y0 in pts]
        pts_dn = [(x0, y0 + hgt) for x0, y0 in reversed(pts)]
        d.polygon(pts_up + pts_dn, fill=color)
    return finish(img)


def aurora(theme, seed=13):
    """Gradient: diagonal navy→violet wash with pink and green discs."""
    # Render the gradient oversized, rotate, then centre-crop so no corner
    # of the frame is left uncovered.
    big = int(max(W, H) * SS * 1.6)
    g = vertical_gradient((big, big), hx(theme['c'][4]), hx(theme['bg']))
    g = g.rotate(18, resample=Image.BICUBIC, expand=False)
    left, top = (big - W * SS) // 2, (big - H * SS) // 2
    img = g.crop((left, top, left + W * SS, top + H * SS))
    d = ImageDraw.Draw(img)
    c = [hx(x) for x in theme['c']]
    rnd = random.Random(seed)
    for i in range(9):
        r = rnd.uniform(70, 260) * SS
        x, y = rnd.uniform(0.05, 0.95) * W * SS, rnd.uniform(0.1, 0.9) * H * SS
        d.ellipse([x - r, y - r, x + r, y + r], fill=rnd.choice([c[0], c[2], c[3], c[5]]))
    for i in range(3):  # thin arcs
        rr = (500 + 260 * i) * SS
        d.arc([W * SS * 0.55 - rr, H * SS * 0.9 - rr, W * SS * 0.55 + rr, H * SS * 0.9 + rr], 190, 350,
              fill=c[2], width=6 * SS)
    return finish(img)


IMAGES = [
    ('plume-light', feather_arcs),
    ('plume-dark', diagonal_bands),
    ('catppuccin-mocha', rounded_tiles),
    ('emerald', hexagons),
    ('sunflower', sun_rays),
    ('deep-sea-light', waves),
    ('deep-sea-dark', lambda t: waves(t, seed=7)),
    ('snowfall', halftone),
    ('steel-grey', bauhaus),
    ('cotton-candy', soft_blobs),
    ('amoled-purple', neon_grid),
    ('high-contrast-yellow', chevrons),
    ('gradient-aurora', aurora),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    ap.add_argument('--sheet', action='store_true', help='also write a contact sheet')
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    tiles = []
    for i, (name, fn) in enumerate(IMAGES):
        img = fn(THEMES[name])
        path = os.path.join(out, f'eink-disable{FIRST_INDEX + i}.jpg')
        img.save(path, 'JPEG', quality=86, optimize=True, progressive=True)
        print(f"{os.path.basename(path):22} {name:22} {os.path.getsize(path) // 1024:5} KB")
        tiles.append(img.resize((640, 400), Image.LANCZOS))
    if args.sheet:
        cols = 4
        rows = math.ceil(len(tiles) / cols)
        sheet = Image.new('RGB', (cols * 650, rows * 410), (40, 40, 40))
        for i, t in enumerate(tiles):
            sheet.paste(t, (5 + (i % cols) * 650, 5 + (i // cols) * 410))
        sheet.save(os.path.join(out, 'contact-sheet.png'))


if __name__ == '__main__':
    main()
