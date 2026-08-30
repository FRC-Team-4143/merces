"""
Store item photo uploads. Files are saved under settings.upload_dir with a random
filename (never the client-supplied one — avoids path traversal and collisions) and
served back out via the /uploads static mount registered in main.py.
"""
import os
import secrets

from fastapi import UploadFile

from app.config import settings

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class InvalidImageError(RuntimeError):
    """Raised when an upload fails type/size validation."""


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


async def save_store_image(file: UploadFile) -> str:
    """Validate and persist an uploaded image. Returns the stored filename (not a path —
    join with settings.upload_dir, or use the /uploads/<filename> URL, to reach it)."""
    ext = _ext(file.filename or "")
    if ext not in ALLOWED_EXTENSIONS or not (file.content_type or "").startswith("image/"):
        raise InvalidImageError("That file doesn't look like a supported image (png/jpg/webp/gif).")

    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise InvalidImageError(f"Image is too large (max {settings.max_upload_mb} MB).")
    if not content:
        raise InvalidImageError("That file is empty.")

    os.makedirs(settings.upload_dir, exist_ok=True)
    filename = f"{secrets.token_hex(16)}{ext}"
    with open(os.path.join(settings.upload_dir, filename), "wb") as f:
        f.write(content)
    return filename


def delete_store_image(filename: str) -> None:
    """Remove a previously saved image. No-op if it's already gone."""
    if not filename:
        return
    try:
        os.remove(os.path.join(settings.upload_dir, filename))
    except FileNotFoundError:
        pass
