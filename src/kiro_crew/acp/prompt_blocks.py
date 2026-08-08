"""Build ACP ``session/prompt`` content blocks from a plain message string.

Channels hand the provider ONE string. When that string contains an absolute
path to a readable image, the image must travel as a real ACP image block --
a bare path is just text, and the model cannot see it. This module owns that
conversion so both prompt paths share one implementation:

* :meth:`kiro_crew.acp.session_handle.AcpSessionHandle.prompt` -- the live path
  for the public Kiro backend (``AcpProvider.start`` swaps ``AcpClient`` out for
  ``AcpSessionProvider``, so this is what actually reaches kiro-cli).
* :meth:`kiro_crew.acp.client.AcpClient._send_prompt` -- the direct-client path.

Keeping one builder matters: both paths need the same path-to-image
conversion, so a single implementation stops any channel from shipping a
filesystem path to the model as text.

Wire shape (per docs/reference/kiro-cli/acp.md):

.. code-block:: json

    {"sessionId": "...", "prompt": [
        {"type": "text",  "text": "look at this [image: shot.png]"},
        {"type": "image", "data": "<base64>", "mimeType": "image/png"}
    ]}
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

from kiro_crew.hooks import is_unc_shape, safe_read_file_bytes, unc_probe_allowed

# The budget constants and Pillow machinery live in the LEAF module
# kiro_crew.imaging (shared with the gateway's tool-result rewrite, which must
# not import the ACP package). The two constants are re-exported because this
# module is where the prompt path's callers and tests historically found them.
from kiro_crew.imaging import (  # noqa: F401 -- constants re-exported, see comment
    MAX_IMAGE_B64_BYTES,
    MAX_IMAGE_EDGE_PX,
    downscale_image_block,
)

logger = logging.getLogger(__name__)

#: Raster formats kiro-cli accepts as inline vision input. SVG is deliberately
#: absent: it is scriptable XML rather than a raster image, and a vision model
#: gains nothing from it.
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: Raw bytes per image, checked BEFORE base64. Encoding inflates by 4/3 and the
#: whole request is serialized as a single newline-delimited JSON frame, so an
#: unbounded image becomes an unbounded write. Matches the Slack producer cap so
#: a file that passed ingestion is not silently dropped here.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: Longest-edge cap (px) enforced on every inlined image. Anthropic rejects the
#: ENTIRE request when a many-image conversation (more than 20 images) carries
#: any image whose width or height exceeds 2000px. Because kiro-cli replays the
#: full message history to the model every turn, a single oversized image
#: permanently wedges the session -- the offending block sits at a fixed history
#: index and re-uploading a smaller copy cannot evict it. This builder is the
#: one funnel every channel's images cross before reaching kiro-cli, so capping
#: here protects all of them (dashboard upload/paste/screenshot, Slack, Discord)
#: regardless of any best-effort client-side resize. 2000 is the hard limit
#: itself: valid even past 20 images, yet it keeps more detail than the browser's
#: 1568px pre-upload downscale, which only ever covers dashboard uploads.
MAX_IMAGE_EDGE_PX = 2000

#: Longest base64-encoded payload (bytes) allowed for a single inlined image.
#: The dimension cap above is not sufficient: a raster can sit well inside 2000px
#: and still encode past the backend's per-image byte ceiling.
#:
#: 5 MiB is the value the backend itself reports. It is not derived from which
#: provider kiro-cli happens to route through, which we treat as opaque, but read
#: straight out of its rejection, which names the limit in bytes:
#:
#:     image exceeds 5 MB maximum: 6714372 bytes > 5242880
#:
#: 5242880 is exactly 5 * 1024 * 1024. (Anthropic's published per-image ceiling
#: for Bedrock and Google Cloud agrees, which is corroboration rather than the
#: basis.) base64 inflates by 4/3, so a ~3.9 MiB raster already exceeds it while
#: passing every pre-encode check.
#:
#: Note this is a DIFFERENT quantity from the limit kiro-cli documents. Its docs
#: state "images must be under 10MB in size" -- a FILE-size rule, which
#: ``MAX_IMAGE_BYTES`` implements -- and say nothing about the encoded payload.
#: Both hold at once: a 5.04 MiB file is under 10 MB yet encodes to 6.71 MiB and
#: is refused. So the encoded ceiling is undocumented but enforced, which is why
#: it has to be observed rather than looked up.
#:
#: Being wrong here is safe in one direction only, which is why the cap is set to
#: the observed value rather than a guess: too LOW merely ships a smaller image,
#: while too HIGH ships a payload the backend refuses. Callers can override it
#: via ``build_prompt_blocks(max_image_b64_bytes=...)`` if a backend ever reports
#: a different number.
#:
#: This is enforced on the ENCODED payload, after any downscale, because that is
#: the only quantity the backend measures: ``MAX_IMAGE_BYTES`` reads the file
#: size before the re-encode and cannot see the encoding overhead. Getting this
#: wrong is not a one-turn error -- a rejected image sits at a fixed history
#: index that kiro-cli replays on every subsequent turn, so one oversized
#: attachment wedges the session permanently.
MAX_IMAGE_B64_BYTES = 5 * 1024 * 1024

#: Floor for the encoded-budget shrink loop. Below ~200px Claude's own guidance
#: says accuracy degrades badly, so an image that still will not fit is dropped
#: to a path reference instead of being shrunk into uselessness.
MIN_IMAGE_EDGE_PX = 256

#: Per-attempt edge multiplier and attempt cap for the encoded-budget shrink.
#: Encoded size falls roughly with area, so 0.8 on the edge sheds ~36% per pass
#: and 6 passes span a 4x linear reduction -- enough to bring any image that the
#: dimension cap admitted under a 5 MiB encoding.
_ENCODE_SHRINK_FACTOR = 0.8
_MAX_ENCODE_ATTEMPTS = 6

#: mime -> Pillow save format for a re-encoded downscale. GIF collapses to a PNG
#: first frame: vision models read frame 0 only (animation is invisible to them)
#: and rescaling a palette image is lossy, so a lossless still is faithful.
_PIL_SAVE_FORMAT: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/bmp": "BMP",
    "image/gif": "PNG",
}

#: Formats every major vision provider accepts natively. Anything outside this
#: set (AVIF, HEIC, TIFF, ICO, …) has to be transcoded to PNG before it is
#: inlined, or the backend returns 400 "Could not process image". Mirrors the
#: wire contract in docs/reference/kiro-cli/acp.md.
_UNIVERSALLY_SUPPORTED_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


def _sniff_mime_from_bytes(raw: bytes) -> str | None:
    """Detect image MIME from magic bytes, or None when unrecognized.

    Filename-suffix detection is unreliable when a channel lies about
    content-type — Discord serves proxied/animated stickers, custom-emoji
    previews and some bot uploads as PNG bytes with ``content_type=image/webp``,
    and Anthropic strictly validates that the declared media type matches the
    actual bytes (HTTP 400 on mismatch). The suffix is only a fallback; the
    bytes are authoritative.
    """
    if not raw:
        return None
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # JPEG: FF D8 FF
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    # GIF87a / GIF89a
    if raw[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    # WEBP: "RIFF" .... "WEBP"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    # BMP: "BM"
    if raw.startswith(b"BM"):
        return "image/bmp"
    # ISO-BMFF family (HEIC/HEIF/AVIF): bytes 4..8 == 'ftyp', major brand at 8..12
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        brand = raw[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {
            b"heic", b"heix", b"hevc", b"hevx",
            b"mif1", b"msf1", b"heim", b"heis",
        }:
            return "image/heic"
    # TIFF: II*\0 (little-endian) or MM\0* (big-endian)
    if raw[:4] in {b"II*\x00", b"MM\x00*"}:
        return "image/tiff"
    # ICO: 00 00 01 00 (reserved=0, type=1=icon)
    if raw[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    # SVG: text-based, look for an <svg tag near the start (skip BOM/whitespace)
    head = raw[:512].lstrip().lower()
    if head.startswith(b"<?xml") or head.startswith(b"<svg"):
        if b"<svg" in head:
            return "image/svg+xml"
    return None


def _transcode_to_png(raw: bytes, mime: str) -> bytes | None:
    """Decode *raw* with Pillow and re-encode as PNG.

    Used for formats outside :data:`_UNIVERSALLY_SUPPORTED_MIMES` — AVIF/HEIC
    (needs an optional Pillow plugin), TIFF, ICO, BMP. Returns None when Pillow
    is missing, cannot decode the bytes, or the format is a vector (SVG), so
    the caller fails CLOSED (keeps the suffix as text) rather than shipping a
    payload the backend refuses.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            # Transparency is preserved where the source had it; a source with
            # no alpha stays in a compact mode instead of being forced to RGBA.
            src = img.convert("RGBA") if img.mode not in {"RGB", "RGBA", "L", "LA", "P"} else img
            buf = io.BytesIO()
            src.save(buf, format="PNG", optimize=False)
            return buf.getvalue()
    except Exception:
        logger.warning(
            "acp prompt: could not transcode %s to PNG; leaving as path", mime, exc_info=True
        )
        return None


# Absolute paths ending in a supported raster suffix.
#
# Two properties are load-bearing, and BOTH were learned from real defects:
#
# 1. The quantifier is non-greedy. A greedy `+` swallows the separator between
#    two paths, so "/tmp/a.png and /tmp/b.png" matched as ONE span ending at the
#    final ".png" -- not a file, so every image in a multi-image message was
#    dropped.
#
# 2. The character class holds HORIZONTAL whitespace only, and a lookbehind
#    forbids starting inside a URL or another path. With `\s` (which includes
#    "\n") a leading URL chained across the newline into the appended path:
#    `slack/events.py` emits "<user text>\n<image path>", so
#
#        see https://example.com/docs\n/tmp/a.png
#
#    matched as "//example.com/docs\n/tmp/a.png" -- one nonexistent path. Any
#    Slack message containing a link therefore lost its image. The `(?<![\w:/])`
#    guard rejects the "/" inside "https://" as a start position, which also
#    stops a URL that merely ends in ".png" from being probed as a local file.
_SUFFIX_GROUP = r"(?:png|jpg|jpeg|gif|webp|bmp)"

#: Space and tab only -- NEVER `\s`. See note 2 above.
_PATH_CHARS = r"[\w./@~ \t()\-]"

#: Must not begin mid-token: rules out "https://host/..." and a "/" that is
#: already part of a longer path.
_NOT_MID_TOKEN = r"(?<![\w:/])"

_POSIX_PATH_RE = re.compile(
    rf"{_NOT_MID_TOKEN}(/{_PATH_CHARS}+?\.{_SUFFIX_GROUP})",
    re.IGNORECASE,
)

# Windows absolute paths: a drive letter ("C:\...", "C:/...") or a UNC share
# ("\\\\host\\share\\..."). Temp attachments land in %LOCALAPPDATA%\Temp and
# dashboard uploads in %USERPROFILE%\.kiro\crew\uploads, so on Windows the
# POSIX grammar matched NOTHING and every image stayed prose -- then the temp
# file was deleted at end of turn, leaving a dead reference.
#
# Platform-gated rather than merged into one pattern: backslash and ":" are
# legal in POSIX filenames, so accepting Windows shapes everywhere makes prose
# like `the path C:\docs\logo.png is an example` a candidate -- and on Linux a
# file with that literal name can exist in the CWD, which would inline a file
# the user only mentioned. Matching the host's own grammar keeps that impossible.
#
# The UNC alternative accepts both separators after the leading pair
# (``\\host\share\...`` and ``//host/share/...``): the dashboard composer
# serializes image attachments with forward slashes (a markdown destination
# cannot carry raw backslashes -- CommonMark eats ``\`` before punctuation),
# and Windows file APIs accept the forward-slash form verbatim. The leading
# pair likewise accepts ``//``; ``(?<![\w:/])`` guards it from matching inside
# a URL's ``://``.
_WINDOWS_PATH_CHARS = r"[\w\\/.@ \t()\-]"
_WINDOWS_PATH_RE = re.compile(
    rf"(?<![\w:])(?:(?<![\w:/]))((?:[A-Za-z]:[\\/]|[\\/]{{2}}[^\\/:*?\"<>|\r\n]+[\\/])"
    rf"{_WINDOWS_PATH_CHARS}+?\.{_SUFFIX_GROUP})",
    re.IGNORECASE,
)

_PATH_RE = _WINDOWS_PATH_RE if os.name == "nt" else _POSIX_PATH_RE


def build_prompt_blocks(
    message: str,
    *,
    allow_image: bool = True,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    max_image_edge: int = MAX_IMAGE_EDGE_PX,
    max_image_b64_bytes: int = MAX_IMAGE_B64_BYTES,
) -> list[dict]:
    """Return ACP prompt blocks for *message*.

    Each readable image path found in *message* becomes an ``image`` block and is
    replaced in the text by ``[image: <name>]`` so the model still sees where the
    attachment sat in the sentence.

    ``allow_image=False`` (the agent did not advertise
    ``promptCapabilities.image``) leaves the path in the text untouched: the file
    is still on disk, so a tool-capable agent can open it, which is a strictly
    better fallback than dropping the reference. The result is always at least
    one text block, so a caller can pass it straight to ``session/prompt``.

    Inlined images are downscaled so their longest edge is at most
    ``max_image_edge`` px -- the server-side backstop for Anthropic's many-image
    dimension limit, applied for EVERY channel here regardless of any
    client-side resize that was skipped or bypassed -- and then shrunk further if
    needed so the base64 payload stays within ``max_image_b64_bytes``, the
    backend's per-image byte ceiling.
    """
    text = message
    images: list[dict] = []

    if allow_image:
        seen: set[str] = set()
        for match in _PATH_RE.finditer(message):
            raw = match.group(1).strip()
            if raw in seen:
                continue
            # UNC-shaped candidates name a HOST on Windows: gate them before
            # any filesystem call, or is_file() below opens an SMB connection
            # to attacker-controlled text. POSIX has no such semantics (a
            # doubled leading slash is an ordinary local path), and _PATH_RE
            # is platform-gated anyway. See kiro_crew.hooks.unc_probe_allowed.
            if os.name == "nt" and is_unc_shape(raw) and not unc_probe_allowed(raw):
                seen.add(raw)
                continue
            path = Path(raw)
            suffix = path.suffix.lower()
            mime = IMAGE_MEDIA_TYPES.get(suffix)
            if mime is None or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                logger.debug("acp prompt: could not stat image %s", raw, exc_info=True)
                continue
            if size > max_image_bytes:
                # Leave the path in the text: the turn still carries a usable
                # reference instead of silently losing the attachment.
                logger.warning(
                    "acp prompt: image %s is %d bytes (cap %d) - sending path, not inline",
                    path.name,
                    size,
                    max_image_bytes,
                )
                continue
            try:
                raw_bytes = safe_read_file_bytes(str(path))
            except Exception:
                logger.debug("acp prompt: could not read image %s", raw, exc_info=True)
                continue
            if raw_bytes is None:
                # Refused by the sensitive-path gate (or unreadable). The path
                # stays in the text; it is NOT inlined.
                logger.warning("acp prompt: image read refused for %s", path.name)
                continue
            # Magic-byte sniff is authoritative: a channel that lies about
            # content-type (Discord serving PNG bytes as webp) would otherwise
            # inline a payload the backend rejects with 400. When the real
            # format differs from the suffix, transcode non-universal formats
            # (AVIF/HEIC/TIFF/ICO/…) to PNG so the declared mime matches the
            # bytes.
            sniffed = _sniff_mime_from_bytes(raw_bytes)
            if sniffed and sniffed != mime:
                logger.debug(
                    "acp prompt: %s declares %s but bytes are %s",
                    path.name,
                    mime,
                    sniffed,
                )
                mime = sniffed
            if mime not in _UNIVERSALLY_SUPPORTED_MIMES:
                transcoded = _transcode_to_png(raw_bytes, mime)
                if transcoded is None:
                    # Fail CLOSED: an untranscodable format (SVG, missing
                    # HEIC/AVIF plugin) stays as a text path rather than
                    # shipping bytes the backend refuses.
                    logger.warning(
                        "acp prompt: image %s is %s and could not be transcoded - "
                        "sending path, not inline",
                        path.name,
                        mime,
                    )
                    continue
                raw_bytes = transcoded
                mime = "image/png"
            downscaled = downscale_image_block(
                raw_bytes,
                mime,
                max_edge=max_image_edge,
                max_b64_bytes=max_image_b64_bytes,
            )
            if downscaled is None:
                # No compliant rendition (decompression-bomb / undecodable /
                # truncated / over the decode-pixel ceiling / still over the
                # encoded ceiling at the minimum edge): leave the path as text
                # rather than inline a payload the backend rejects on this and
                # every later turn. A tool-capable agent can still open it.
                logger.warning(
                    "acp prompt: image %s could not be rendered within the "
                    "dimension and encoded-size caps - sending path, not inline",
                    path.name,
                )
                continue
            out_bytes, out_mime = downscaled
            data = base64.b64encode(out_bytes).decode("ascii")
            seen.add(raw)
            images.append({"type": "image", "data": data, "mimeType": out_mime})
            text = text.replace(raw, f"[image: {path.name}]")

    return [{"type": "text", "text": text}, *images]
