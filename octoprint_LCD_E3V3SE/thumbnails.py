"""Read embedded PNG/JPEG previews without relying on a slicer identification line."""
import base64
import binascii
import io
import itertools
import re

from PIL import Image


EXTRACTION_VERSION = 2
_BEGIN = re.compile(r"thumbnail(?:_(PNG|JPG|JPEG))?\s+begin\s+(\d+)x(\d+)(?:\s+\d+)?", re.IGNORECASE)
_END = re.compile(r"thumbnail(?:_(PNG|JPG|JPEG))?\s+end", re.IGNORECASE)
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_MAX_ENCODED_SIZE = 4 * 1024 * 1024


def _image_runs(lines):
    """Yield comment payloads and whether they have a complete 96x96 header."""
    header, parts, size = None, [], 0
    for line in itertools.chain(lines, ("",)):
        line = line.strip()
        comment = line[1:].strip() if line.startswith(";") else None
        if comment and _BASE64.fullmatch(comment):
            if parts or header or comment.startswith(("iVBORw0KGgo", "/9j/")):
                size += len(comment)
                if size <= _MAX_ENCODED_SIZE:
                    parts.append(comment)
                else:
                    parts = []
                continue
        if parts:
            end = _END.fullmatch(comment or "")
            standard = bool(header and header[1:] == (96, 96) and end
                            and (end.group(1) or "").upper() == header[0])
            yield "".join(parts), standard
            header, parts, size = None, [], 0
        begin = _BEGIN.fullmatch(comment or "")
        if begin:
            header = ((begin.group(1) or "").upper(), int(begin.group(2)), int(begin.group(3)))
        elif comment != "":
            header = None
            size = 0


def extract_thumbnail(lines, allow_fallback=False):
    """Read a valid 96x96 image, or recover the largest embedded PNG/JPEG.

    A larger source retains detail when the print-card pipeline crops margins
    and fits the model into 96x96. Fallback also accepts bare Base64 comments.
    """
    best, best_area = None, 0
    for payload, standard in _image_runs(lines):
        if not standard and not allow_fallback:
            continue
        try:
            with Image.open(io.BytesIO(base64.b64decode(payload, validate=True))) as image:
                if image.format not in ("PNG", "JPEG"):
                    continue
                width, height = image.size
                image.load()
        except (ValueError, OSError, binascii.Error, Image.DecompressionBombError):
            continue
        if not allow_fallback and (width, height) == (96, 96):
            return payload
        if allow_fallback and width * height > best_area:
            best, best_area = payload, width * height
    return best
