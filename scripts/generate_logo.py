"""Regenerate the ncglobe logo and common icon sizes (requires Pillow)."""
from pathlib import Path
from math import cos, sin, radians

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / 'src/ncglobe/static'
SIZES = (16, 24, 32, 48, 64, 128, 256)
NAVY = '#122b3e'
MINT = '#4ce0ba'
WHITE = '#f4faf9'


def generate():
    OUT.mkdir(parents=True, exist_ok=True)
    scale = 4
    im = Image.new('RGBA', (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(im)
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" fill="none">',
        '  <title>ncglobe</title>',
        '  <desc>Balanced lowercase nc lettering on a dark globe with a mint observation orbit.</desc>',
        f'  <circle cx="128" cy="128" r="100" fill="{NAVY}"/>',
    ]

    def bounds(values):
        return tuple(round(v * scale) for v in values)

    def circle(x, y, radius, color):
        draw.ellipse(bounds((x-radius, y-radius, x+radius, y+radius)), fill=color)

    def stroke(points, width, color):
        draw.line([bounds(p) for p in points], fill=color, width=round(width*scale), joint='curve')
        for x, y in (points[0], points[-1]):
            circle(x, y, width / 2, color)

    def cubic(a, b, c, d):
        return [tuple((1-t)**3*a[j]+3*(1-t)**2*t*b[j]+3*(1-t)*t*t*c[j]+t**3*d[j]
                      for j in (0, 1)) for t in (i/160 for i in range(161))]

    circle(128, 128, 100, NAVY)
    # Both letters share a 58-unit x-height and 13-unit stroke. The open c
    # balances the n's heavier verticals without stretching either letter.
    svg.append(f'  <path d="M68 151.5V93.5M68 116C68 86 116 86 116 116V151.5" stroke="{WHITE}" stroke-width="13" stroke-linecap="round"/>')
    stroke([(68, 151.5), (68, 93.5)], 13, WHITE)
    stroke(cubic((68,116), (68,86), (116,86), (116,116)) + [(116,151.5)], 13, WHITE)
    arc = [(166 + 27*cos(radians(-50-i*260/160)),
            122.5 + 29*sin(radians(-50-i*260/160))) for i in range(161)]
    x1, y1 = arc[0]
    x2, y2 = arc[-1]
    svg.append(f'  <path d="M{x1:.6f} {y1:.6f}A27 29 0 1 0 {x2:.6f} {y2:.6f}" stroke="{WHITE}" stroke-width="13" stroke-linecap="round"/>')
    stroke(arc, 13, WHITE)
    svg.append(f'  <path d="M37 164C8 204 160 210 224 140" stroke="{NAVY}" stroke-width="16" stroke-linecap="round"/>')
    orbit = cubic((37,164), (8,204), (160,210), (224,140))
    stroke(orbit, 16, NAVY)
    svg.append(f'  <path d="M37 164C8 204 160 210 224 140" stroke="{MINT}" stroke-width="8" stroke-linecap="round"/>')
    stroke(orbit, 8, MINT)
    svg.append(f'  <circle cx="205" cy="64" r="8" fill="{MINT}"/>')
    circle(205, 64, 8, MINT)
    svg.append('</svg>')
    (OUT / 'logo.svg').write_text('\n'.join(svg) + '\n')
    im.resize((256, 256), Image.Resampling.LANCZOS).save(OUT / 'favicon.ico', sizes=[(s, s) for s in SIZES])
    for size in (180, 192, 512):
        im.resize((size, size), Image.Resampling.LANCZOS).save(OUT / f'logo-{size}.png')
    im.save(OUT / 'logo-1024.png')   # macOS App 圖示用(scripts/build_exe.py 裁掉留白再放大)


if __name__ == '__main__':
    generate()
