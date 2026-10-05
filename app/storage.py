"""Photo storage.

Production: Azure Blob Storage via Managed Identity (no account keys, no SAS).
The identity needs the "Storage Blob Data Contributor" role on the account,
and shared-key access should be disabled on the storage account.

Development: a local folder (only allowed when APP_ENV=development).

Blob names are server-generated UUIDs; user-supplied filenames are never used.
Customer uploads are stored under the user's Entra object ID so one customer
can never read another customer's photo.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Protocol

from .config import Settings
from .schemas import ReferenceTyre

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_OWNER_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")


def new_id() -> str:
    return uuid.uuid4().hex


def _check_id(value: str) -> str:
    if not _ID_RE.match(value):
        raise KeyError("invalid id")
    return value


def _check_owner(value: str) -> str:
    if not _OWNER_RE.match(value):
        raise KeyError("invalid owner")
    return value


class PhotoStore(Protocol):
    def save_reference(self, ref: ReferenceTyre, jpeg: bytes) -> None: ...
    def list_references(self) -> list[ReferenceTyre]: ...
    def get_reference_image(self, ref_id: str) -> bytes: ...
    def delete_reference(self, ref_id: str) -> None: ...
    def save_upload(self, owner: str, upload_id: str, jpeg: bytes) -> None: ...


class LocalPhotoStore:
    def __init__(self, root: str) -> None:
        self.refs = Path(root, "references")
        self.uploads = Path(root, "uploads")
        self.refs.mkdir(parents=True, exist_ok=True)
        self.uploads.mkdir(parents=True, exist_ok=True)

    def save_reference(self, ref: ReferenceTyre, jpeg: bytes) -> None:
        rid = _check_id(ref.id)
        (self.refs / f"{rid}.jpg").write_bytes(jpeg)
        (self.refs / f"{rid}.json").write_text(ref.model_dump_json(), encoding="utf-8")

    def list_references(self) -> list[ReferenceTyre]:
        return [
            ReferenceTyre.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self.refs.glob("*.json"))
        ]

    def get_reference_image(self, ref_id: str) -> bytes:
        path = self.refs / f"{_check_id(ref_id)}.jpg"
        if not path.exists():
            raise KeyError(ref_id)
        return path.read_bytes()

    def delete_reference(self, ref_id: str) -> None:
        rid = _check_id(ref_id)
        for suffix in (".jpg", ".json"):
            (self.refs / f"{rid}{suffix}").unlink(missing_ok=True)

    def save_upload(self, owner: str, upload_id: str, jpeg: bytes) -> None:
        folder = self.uploads / _check_owner(owner)
        folder.mkdir(exist_ok=True)
        (folder / f"{_check_id(upload_id)}.jpg").write_bytes(jpeg)


class BlobPhotoStore:
    def __init__(self, account_url: str, reference_container: str, upload_container: str,
                 credential) -> None:
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(account_url=account_url, credential=credential)
        self.refs = service.get_container_client(reference_container)
        self.uploads = service.get_container_client(upload_container)

    def save_reference(self, ref: ReferenceTyre, jpeg: bytes) -> None:
        from azure.storage.blob import ContentSettings

        rid = _check_id(ref.id)
        self.refs.upload_blob(f"{rid}.jpg", jpeg, overwrite=False,
                              content_settings=ContentSettings(content_type="image/jpeg"))
        self.refs.upload_blob(f"{rid}.json", ref.model_dump_json().encode(), overwrite=True,
                              content_settings=ContentSettings(content_type="application/json"))

    def list_references(self) -> list[ReferenceTyre]:
        items = []
        for blob in self.refs.list_blobs():
            if blob.name.endswith(".json"):
                raw = self.refs.download_blob(blob.name).readall()
                items.append(ReferenceTyre.model_validate(json.loads(raw)))
        return items

    def get_reference_image(self, ref_id: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.refs.download_blob(f"{_check_id(ref_id)}.jpg").readall()
        except ResourceNotFoundError as exc:
            raise KeyError(ref_id) from exc

    def delete_reference(self, ref_id: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        rid = _check_id(ref_id)
        for name in (f"{rid}.jpg", f"{rid}.json"):
            try:
                self.refs.delete_blob(name)
            except ResourceNotFoundError:
                pass

    def save_upload(self, owner: str, upload_id: str, jpeg: bytes) -> None:
        from azure.storage.blob import ContentSettings

        name = f"{_check_owner(owner)}/{_check_id(upload_id)}.jpg"
        self.uploads.upload_blob(name, jpeg, overwrite=False,
                                 content_settings=ContentSettings(content_type="image/jpeg"))


def build_photo_store(settings: Settings, credential) -> PhotoStore:
    if settings.storage_account_url:
        return BlobPhotoStore(settings.storage_account_url, settings.reference_container,
                              settings.upload_container, credential)
    if settings.is_production:
        raise RuntimeError("STORAGE_ACCOUNT_URL is required in production")
    return LocalPhotoStore(settings.local_data_dir)
