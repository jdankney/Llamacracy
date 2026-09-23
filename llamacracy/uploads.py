# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image uploads for on-demand vision. Saved to disk under
UPLOAD_DIR -- never as base64 in the database. Only a path + mime + size are
stored, scoped to the uploading user; served back only to them (app.py).
"""

from __future__ import annotations

import uuid
from pathlib import Path

# llama.cpp's clip/mmproj vision towers are trained on these; anything else
# (gif, bmp, ...) either isn't decodable or isn't worth the risk of a silent
# single-frame guess.
ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class UploadRejected(Exception):
    pass


def validate(content_type: str | None, size: int, max_mb: float) -> str:
    """Returns the file extension to use, or raises UploadRejected."""
    ext = ALLOWED_MIME.get((content_type or "").split(";")[0].strip().lower())
    if ext is None:
        raise UploadRejected(
            f"unsupported image type: {content_type!r} (png, jpeg, webp only)")
    if size <= 0:
        raise UploadRejected("empty file")
    if size > max_mb * 1024 * 1024:
        raise UploadRejected(f"image too large ({size / 1e6:.1f} MB > {max_mb:.0f} MB)")
    return ext


def save(upload_dir: str, data: bytes, ext: str) -> tuple[str, str]:
    """Writes `data` to a new uuid-named file under upload_dir.
    Returns (id, path)."""
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    path = Path(upload_dir) / f"{file_id}{ext}"
    path.write_bytes(data)
    return file_id, str(path)
