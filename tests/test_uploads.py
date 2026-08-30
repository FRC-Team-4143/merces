"""Store item photo validation + save/delete (services/uploads.py), isolated from the
HTTP layer via a monkeypatched settings.upload_dir pointed at a tmp_path."""
from io import BytesIO

import pytest
from starlette.datastructures import Headers, UploadFile

from app.config import settings
from app.services import uploads as uploads_service


def _make_upload(filename, content, content_type="image/png"):
    return UploadFile(
        file=BytesIO(content), filename=filename,
        headers=Headers({"content-type": content_type}),
    )


@pytest.mark.asyncio
async def test_rejects_unsupported_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    upload = _make_upload("virus.exe", b"whatever", "application/octet-stream")
    with pytest.raises(uploads_service.InvalidImageError):
        await uploads_service.save_store_image(upload)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rejects_mismatched_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    # Right extension, wrong declared content-type — still rejected.
    upload = _make_upload("photo.png", b"fake", "text/plain")
    with pytest.raises(uploads_service.InvalidImageError):
        await uploads_service.save_store_image(upload)


@pytest.mark.asyncio
async def test_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    upload = _make_upload("photo.png", b"x" * (2 * 1024 * 1024), "image/png")
    with pytest.raises(uploads_service.InvalidImageError):
        await uploads_service.save_store_image(upload)


@pytest.mark.asyncio
async def test_rejects_empty_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    upload = _make_upload("photo.png", b"", "image/png")
    with pytest.raises(uploads_service.InvalidImageError):
        await uploads_service.save_store_image(upload)


@pytest.mark.asyncio
async def test_saves_with_a_random_filename_not_the_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    upload = _make_upload("../../etc/passwd.png", b"pixel-bytes", "image/png")
    filename = await uploads_service.save_store_image(upload)
    assert filename != "../../etc/passwd.png"
    assert "/" not in filename
    assert filename.endswith(".png")
    saved = tmp_path / filename
    assert saved.exists()
    assert saved.read_bytes() == b"pixel-bytes"


def test_delete_removes_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    f = tmp_path / "gone.png"
    f.write_bytes(b"x")
    uploads_service.delete_store_image("gone.png")
    assert not f.exists()


def test_delete_is_a_noop_when_missing_or_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    uploads_service.delete_store_image("never-existed.png")  # must not raise
    uploads_service.delete_store_image(None)
    uploads_service.delete_store_image("")
