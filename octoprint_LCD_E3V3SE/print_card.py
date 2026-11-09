"""Host-side print-card composition. Coordinates leave native controls untouched."""
from collections import defaultdict
from .drawing import color, rect

GREEN = 0x07E0
# Compact 5x7 glyphs, independent of host fonts and LCD resource revisions.
GLYPHS = {
    "A": (14,17,17,31,17,17,17), "D": (30,17,17,17,17,17,30),
    "E": (31,16,16,30,16,16,31), "G": (14,17,16,23,17,17,15),
    "H": (17,17,17,31,17,17,17), "I": (31,4,4,4,4,4,31),
    "L": (16,16,16,16,16,16,31), "M": (17,27,21,21,17,17,17),
    "O": (14,17,17,17,17,17,14), "P": (30,17,17,30,16,16,16),
    "R": (30,17,17,30,20,18,17), "S": (15,16,16,14,1,1,30),
    "T": (31,4,4,4,4,4,4), "Y": (17,17,10,4,4,4,4),
}


def merged_rectangles(pixels, width, height, x=0, y=0):
    """Exact horizontal runs, vertical span merging, disjoint color buckets."""
    if len(pixels) != width * height:
        raise ValueError("Invalid pixel count")
    previous, output = {}, []
    for row in range(height):
        current, col = {}, 0
        while col < width:
            start, value = col, pixels[row * width + col]
            col += 1
            while col < width and pixels[row * width + col] == value:
                col += 1
            if value == 0:
                continue
            key = (start, col - 1, value)
            run = previous.pop(key, None)
            if run is None:
                run = [x + start, y + row, x + col - 1, y + row, value]
            else:
                run[3] = y + row
            current[key] = run
        output.extend(previous.values())
        previous = current
    output.extend(previous.values())
    groups = defaultdict(list)
    for x0, y0, x1, y1, value in output:
        groups[value].append(rect(x0, y0, x1, y1))
    for value in sorted(groups):
        yield color(value)
        yield from groups[value]


def thumbnail_operations(pixels):
    runs = list(merged_rectangles(pixels,96,96,12,26))
    foreground = sum(c != 0 for c in pixels)
    # STRIP is used only when it cannot increase LCD work. Shape/icon artwork
    # keeps the much cheaper merged-rectangle path.
    if len(runs) < foreground * 2:
        return runs
    strips = []
    for row in range(96):
        x = 0
        while x < 96:
            if not pixels[row*96+x]:
                x += 1
                continue
            start = x
            while x < 96 and x-start < 19 and pixels[row*96+x]:
                x += 1
            op = bytes((2,12+start,26+row,x-start))
            op += b"".join(int(c).to_bytes(2,"big") for c in pixels[row*96+start:row*96+x])
            strips.append(op)
    return strips if sum(map(len,strips)) < sum(map(len,runs)) else runs


def label(text, x, y):
    pixels = [0] * (len(text) * 6 * 7)
    width = len(text) * 6
    for i, char in enumerate(text):
        for row, bits in enumerate(GLYPHS[char]):
            for col in range(5):
                if bits & (16 >> col):
                    pixels[row * width + i * 6 + col] = GREEN
    return merged_rectangles(pixels, width, 7, x, y)


def card_operations(pixels=None):
    # Only the thumbnail belongs to the host. Keep native progress on the right.
    ops = [color(0), rect(10, 25, 109, 122)]
    if pixels is not None:
        ops.extend(thumbnail_operations(pixels))
    else:
        # Missing/invalid thumbnails have a small neutral host-drawn placeholder.
        ops.extend(label("MODEL",44,68))
    # Draw the complete border last, below the scrolling title (ending at y=24)
    # and above Tune's clear/scroll area (starting at y=123).
    ops += [color(GREEN), rect(10,25,109,25), rect(10,122,109,122),
            rect(10,25,10,122), rect(109,25,109,122)]
    return ops


def rgb565(image):
    """Resize on the host, compositing alpha over the initialized black region."""
    from PIL import Image
    source = image.convert("RGBA")
    source.thumbnail((96,96), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA",(96,96),(0,0,0,255))
    canvas.alpha_composite(source,((96-source.width)//2,(96-source.height)//2))
    return [((r*31//255)<<11)|((g*63//255)<<5)|(b*31//255)
            for r,g,b in canvas.convert("RGB").getdata()]
