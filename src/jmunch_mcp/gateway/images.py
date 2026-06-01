"""Optional image compression for outbound requests.

Detects base64-encoded image content blocks in OpenAI and Anthropic
request shapes, decodes them, downsizes if larger than a configured max
dimension, and re-encodes as JPEG at a configurable quality. HTTP/HTTPS
image URLs are left untouched (we don't fetch them just to transform).

Requires the optional `[images]` extra (Pillow). When Pillow isn't
importable, both `compress_openai_messages` and `compress_anthropic_
messages` no-op so the route paths stay safe.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("jmunch.gateway.images")

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:  # pragma: no cover
    _HAVE_PIL = False


@dataclass
class ImageSettings:
    enabled: bool = False
    max_dimension: int = 1568        # Anthropic's recommended max long-edge
    quality: int = 85                # JPEG quality on re-encode


_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", re.DOTALL)


def _compress_bytes(data: bytes, *, max_dimension: int, quality: int) -> bytes | None:
    """Return new JPEG bytes, or None if no benefit / decode failed.

    Always returns smaller output than the input — if re-encode wouldn't
    save anything (small PNG with text, already-tiny JPEG, etc.) we return
    None and the caller keeps the original."""
    if not _HAVE_PIL:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        log.debug("image decode failed, leaving unchanged: %s", e)
        return None

    orig_w, orig_h = img.width, img.height
    if max(img.width, img.height) > max_dimension:
        img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    # JPEG can't carry alpha — composite onto white when there's transparency.
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.split()[-1]
        bg.paste(img.convert("RGBA"), mask=alpha)
        img = bg
    elif img.mode == "P":
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    out = buf.getvalue()

    if len(out) >= len(data):
        return None

    log.info(
        "image compressed: %dx%d→%dx%d, %d→%d bytes (%.1f%% saved)",
        orig_w, orig_h, img.width, img.height, len(data), len(out),
        (1 - len(out) / len(data)) * 100,
    )
    return out


def _process_data_url(url: str, settings: ImageSettings) -> str | None:
    m = _DATA_URL_RE.match(url or "")
    if not m:
        return None
    try:
        data = base64.b64decode(m.group(1))
    except Exception:
        return None
    out = _compress_bytes(data, max_dimension=settings.max_dimension, quality=settings.quality)
    if out is None:
        return None
    return f"data:image/jpeg;base64,{base64.b64encode(out).decode('ascii')}"


def compress_openai_messages(
    messages: list[Any], settings: ImageSettings,
) -> tuple[list[Any], int]:
    """Walk an OpenAI `messages` list and compress `image_url` data-URLs.

    Returns (new_messages, bytes_saved). The original `messages` and its
    dicts are never mutated."""
    if not _HAVE_PIL or not settings.enabled or not isinstance(messages, list):
        return messages, 0

    saved = 0
    out_msgs: list[Any] = []
    any_change = False
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            out_msgs.append(m)
            continue
        new_content: list[Any] = []
        touched = False
        for block in m["content"]:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                new_content.append(block)
                continue
            img_field = block.get("image_url") or {}
            url = img_field.get("url", "")
            new_url = _process_data_url(url, settings)
            if not new_url or new_url == url:
                new_content.append(block)
                continue
            saved += len(url) - len(new_url)
            new_block = dict(block)
            new_block["image_url"] = {**img_field, "url": new_url}
            new_content.append(new_block)
            touched = True
        if touched:
            new_m = dict(m)
            new_m["content"] = new_content
            out_msgs.append(new_m)
            any_change = True
        else:
            out_msgs.append(m)
    return (out_msgs if any_change else messages), saved


def compress_anthropic_messages(
    messages: list[Any], settings: ImageSettings,
) -> tuple[list[Any], int]:
    """Walk an Anthropic `messages` list and compress base64 `image` blocks."""
    if not _HAVE_PIL or not settings.enabled or not isinstance(messages, list):
        return messages, 0

    saved = 0
    out_msgs: list[Any] = []
    any_change = False
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            out_msgs.append(m)
            continue
        new_content: list[Any] = []
        touched = False
        for block in m["content"]:
            if not isinstance(block, dict) or block.get("type") != "image":
                new_content.append(block)
                continue
            src = block.get("source") or {}
            if src.get("type") != "base64":
                new_content.append(block)
                continue
            try:
                data = base64.b64decode(src.get("data", ""))
            except Exception:
                new_content.append(block)
                continue
            out = _compress_bytes(data, max_dimension=settings.max_dimension,
                                  quality=settings.quality)
            if out is None:
                new_content.append(block)
                continue
            saved += len(data) - len(out)
            new_block = dict(block)
            new_block["source"] = {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(out).decode("ascii"),
            }
            new_content.append(new_block)
            touched = True
        if touched:
            new_m = dict(m)
            new_m["content"] = new_content
            out_msgs.append(new_m)
            any_change = True
        else:
            out_msgs.append(m)
    return (out_msgs if any_change else messages), saved
