"""Image compression: detect base64 images in OpenAI / Anthropic
requests, downsize + re-encode if oversized. HTTPS URLs untouched.

Skips automatically when the [images] extra (Pillow) isn't installed."""
from __future__ import annotations

import base64
import io

import pytest

pytest.importorskip("PIL")

from PIL import Image

from jmunch_mcp.gateway.images import (
    ImageSettings,
    _compress_bytes,
    compress_anthropic_messages,
    compress_openai_messages,
)


# --- helpers ------------------------------------------------------------

def _make_png(width: int, height: int, *, mode: str = "RGB") -> bytes:
    """A high-entropy PNG — random pixels. PNG's LZ77 can't compress
    random data, so the file is large; JPEG-after-downsize beats it
    comfortably. (A striped/uniform PNG compresses so tightly that no
    downscaled JPEG can match it; tests would then mis-fire.)"""
    import os
    channels = 4 if mode in ("RGBA", "LA") else 3
    img = Image.frombytes(mode, (width, height), os.urandom(width * height * channels))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()


# --- _compress_bytes ----------------------------------------------------

def test_compress_bytes_downsizes_oversized_image():
    png = _make_png(3000, 3000)
    out = _compress_bytes(png, max_dimension=1568, quality=85)
    assert out is not None
    assert len(out) < len(png)
    # confirm the new image really IS within the dimension cap
    re_img = Image.open(io.BytesIO(out))
    assert max(re_img.width, re_img.height) <= 1568


def test_compress_bytes_handles_rgba_by_compositing_on_white():
    png = _make_png(2000, 2000, mode="RGBA")
    out = _compress_bytes(png, max_dimension=1568, quality=85)
    assert out is not None
    # Must be a valid RGB JPEG now (no alpha — JPEG can't carry it).
    re_img = Image.open(io.BytesIO(out))
    assert re_img.mode == "RGB"


# --- compress_openai_messages -------------------------------------------

def test_openai_compresses_oversized_data_url():
    png = _make_png(3000, 3000)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what's this?"},
        {"type": "image_url", "image_url": {"url": _data_url(png), "detail": "auto"}},
    ]}]
    out, saved = compress_openai_messages(msgs, ImageSettings(enabled=True))
    assert saved > 0
    new_url = out[0]["content"][1]["image_url"]["url"]
    assert new_url.startswith("data:image/jpeg;base64,")
    # `detail` and other fields must survive the rewrite.
    assert out[0]["content"][1]["image_url"]["detail"] == "auto"
    # Original messages list is not mutated.
    assert msgs[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_leaves_https_urls_untouched():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
    ]}]
    out, saved = compress_openai_messages(msgs, ImageSettings(enabled=True))
    assert saved == 0
    assert out is msgs   # no rewrite at all


def test_openai_skips_when_disabled():
    png = _make_png(3000, 3000)
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": _data_url(png)}},
    ]}]
    out, saved = compress_openai_messages(msgs, ImageSettings(enabled=False))
    assert saved == 0
    assert out is msgs


def test_openai_leaves_non_image_content_alone():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    out, saved = compress_openai_messages(msgs, ImageSettings(enabled=True))
    assert saved == 0
    assert out is msgs


# --- compress_anthropic_messages ----------------------------------------

def test_anthropic_compresses_oversized_base64_block():
    png = _make_png(3000, 3000)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(png).decode(),
        }},
    ]}]
    out, saved = compress_anthropic_messages(msgs, ImageSettings(enabled=True))
    assert saved > 0
    new_src = out[0]["content"][1]["source"]
    assert new_src["type"] == "base64"
    assert new_src["media_type"] == "image/jpeg"   # re-encoded as JPEG
    re_bytes = base64.b64decode(new_src["data"])
    assert len(re_bytes) < len(png)


def test_anthropic_leaves_url_source_untouched():
    msgs = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "url",
                                     "url": "https://example.com/img.png"}},
    ]}]
    out, saved = compress_anthropic_messages(msgs, ImageSettings(enabled=True))
    assert saved == 0
    assert out is msgs


def test_anthropic_skips_when_disabled():
    png = _make_png(3000, 3000)
    msgs = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png",
                                     "data": base64.b64encode(png).decode()}},
    ]}]
    out, saved = compress_anthropic_messages(msgs, ImageSettings(enabled=False))
    assert saved == 0
    assert out is msgs


# --- malformed input safety --------------------------------------------

def test_garbage_base64_does_not_crash_openai():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!not-base64!!!"}},
    ]}]
    out, saved = compress_openai_messages(msgs, ImageSettings(enabled=True))
    assert saved == 0
    assert out is msgs


def test_garbage_base64_does_not_crash_anthropic():
    msgs = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png",
                                     "data": "!!!not-base64!!!"}},
    ]}]
    out, saved = compress_anthropic_messages(msgs, ImageSettings(enabled=True))
    assert saved == 0
    assert out is msgs
