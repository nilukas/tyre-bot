"""Safe handling of uploaded photos (OWASP File Upload Cheat Sheet).

Every upload is decoded and re-encoded by Pillow:
  * the real format is detected from the bytes, not the filename or Content-Type,
  * decompression bombs are rejected,
  * EXIF (including GPS location) and any trailing/polyglot payload are dropped,
  * the image is resized to the size Claude's vision works best with.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}  # MPO = multi-picture JPEG from some phones
MAX_PIXELS = 40_000_000
MAX_EDGE = 1568  # longest edge recommended for Claude vision
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class InvalidImage(ValueError):
    pass


def sanitize_image(data: bytes, *, max_bytes: int) -> bytes:
    """Validate an uploaded image and return a clean JPEG."""
    if not data:
        raise InvalidImage("Empty file.")
    if len(data) > max_bytes:
        raise InvalidImage(f"Image is larger than {max_bytes // (1024 * 1024)} MB.")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ALLOWED_FORMATS:
                raise InvalidImage("Only JPEG, PNG or WebP photos are accepted.")
            probe.verify()  # structural check; image is unusable afterwards

        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise InvalidImage("Image dimensions are too large.")
            img = ImageOps.exif_transpose(img)  # honour phone rotation, then drop EXIF
            img = img.convert("RGB")
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=90, optimize=True)  # no exif= -> metadata stripped
            return out.getvalue()
    except InvalidImage:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise InvalidImage("The file is not a valid image.") from exc
