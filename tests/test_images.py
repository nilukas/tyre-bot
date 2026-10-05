import io

import pytest
from PIL import Image

from app.images import InvalidImage, sanitize_image

MAX = 10 * 1024 * 1024


def _png(w=3000, h=2000) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 40, 40)).save(buf, format="PNG")
    return buf.getvalue()


def test_reencodes_to_small_jpeg():
    out = sanitize_image(_png(), max_bytes=MAX)
    with Image.open(io.BytesIO(out)) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 1568
        assert not img.getexif()


def test_rejects_non_image():
    with pytest.raises(InvalidImage):
        sanitize_image(b"<script>alert(1)</script>", max_bytes=MAX)


def test_rejects_oversize():
    with pytest.raises(InvalidImage):
        sanitize_image(b"x" * 101, max_bytes=100)


def test_rejects_disallowed_format():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="GIF")
    with pytest.raises(InvalidImage):
        sanitize_image(buf.getvalue(), max_bytes=MAX)
