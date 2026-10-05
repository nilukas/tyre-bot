"""Data storage: photos, reference labels, analyses and chat history.

Pick the backend with DATA_BACKEND:

* ``azure_blob``  (production) Azure Blob Storage via Managed Identity (no keys, no SAS).
  The identity needs "Storage Blob Data Contributor"; disable shared-key access.
  Analyses/chat are kept in memory (move to Cosmos DB for multi-instance).
* ``sqlserver``   (development) everything in a local SQL Server database, created
  automatically on startup. See ``sqlserver_store.py``.
* ``local_files`` (development) everything in a local folder as .jpg + .json files.

Ids are server-generated UUIDs; user-supplied filenames are never used. Customer data
is always keyed by the user's Entra object ID so one customer can never read another's.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Protocol

from .config import Settings
from .schemas import ReferenceTyre

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_OWNER_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")


def new_id() -> str:
    return uuid.uuid4().hex


def check_id(value: str) -> str:
    if not _ID_RE.match(value):
        raise KeyError("invalid id")
    return value


def check_owner(value: str) -> str:
    if not _OWNER_RE.match(value):
        raise KeyError("invalid owner")
    return value


class DataStore(Protocol):
    # Reference library (staff)
    def save_reference(self, ref: ReferenceTyre, jpeg: bytes) -> None: ...
    def list_references(self) -> list[ReferenceTyre]: ...
    def get_reference_image(self, ref_id: str) -> bytes: ...
    def delete_reference(self, ref_id: str) -> None: ...

    # Customer photos and analyses
    def save_upload(self, owner: str, upload_id: str, jpeg: bytes) -> None: ...
    def save_analysis(self, owner: str, analysis_id: str, vehicle_type: str,
                      assessment: dict, estimate: dict) -> None: ...
    def get_analysis(self, owner: str, analysis_id: str) -> dict | None:
        """Return {"assessment": dict, "estimate": dict, "history": [{"role", "content"}]}."""
    def append_messages(self, owner: str, analysis_id: str, messages: list[dict]) -> None: ...


# --------------------------------------------------------------------------
# In-memory analyses (used with Blob Storage)
# --------------------------------------------------------------------------


class _MemoryAnalyses:
    MAX_PER_OWNER = 20

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def save_analysis(self, owner, analysis_id, vehicle_type, assessment, estimate) -> None:
        with self._lock:
            mine = [k for k in self._data if k[0] == owner]
            for k in mine[: max(0, len(mine) - self.MAX_PER_OWNER + 1)]:
                del self._data[k]
            self._data[(owner, analysis_id)] = {
                "assessment": assessment, "estimate": estimate, "history": []}

    def get_analysis(self, owner, analysis_id):
        with self._lock:
            rec = self._data.get((owner, analysis_id))
            return None if rec is None else {**rec, "history": list(rec["history"])}

    def append_messages(self, owner, analysis_id, messages) -> None:
        with self._lock:
            self._data[(owner, analysis_id)]["history"].extend(messages)


# --------------------------------------------------------------------------
# Local folder
# --------------------------------------------------------------------------


class LocalFileStore:
    def __init__(self, root: str) -> None:
        self.refs = Path(root, "references")
        self.uploads = Path(root, "uploads")
        self.analyses = Path(root, "analyses")
        for p in (self.refs, self.uploads, self.analyses):
            p.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save_reference(self, ref: ReferenceTyre, jpeg: bytes) -> None:
        rid = check_id(ref.id)
        (self.refs / f"{rid}.jpg").write_bytes(jpeg)
        (self.refs / f"{rid}.json").write_text(ref.model_dump_json(), encoding="utf-8")

    def list_references(self) -> list[ReferenceTyre]:
        return [ReferenceTyre.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(self.refs.glob("*.json"))]

    def get_reference_image(self, ref_id: str) -> bytes:
        path = self.refs / f"{check_id(ref_id)}.jpg"
        if not path.exists():
            raise KeyError(ref_id)
        return path.read_bytes()

    def delete_reference(self, ref_id: str) -> None:
        rid = check_id(ref_id)
        for suffix in (".jpg", ".json"):
            (self.refs / f"{rid}{suffix}").unlink(missing_ok=True)

    def save_upload(self, owner: str, upload_id: str, jpeg: bytes) -> None:
        folder = self.uploads / check_owner(owner)
        folder.mkdir(exist_ok=True)
        (folder / f"{check_id(upload_id)}.jpg").write_bytes(jpeg)

    def _analysis_path(self, owner: str, analysis_id: str) -> Path:
        return self.analyses / check_owner(owner) / f"{check_id(analysis_id)}.json"

    def save_analysis(self, owner, analysis_id, vehicle_type, assessment, estimate) -> None:
        path = self._analysis_path(owner, analysis_id)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"vehicle_type": vehicle_type, "assessment": assessment,
                                    "estimate": estimate, "history": []}), encoding="utf-8")

    def get_analysis(self, owner, analysis_id):
        try:
            path = self._analysis_path(owner, analysis_id)
        except KeyError:
            return None
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def append_messages(self, owner, analysis_id, messages) -> None:
        with self._lock:
            path = self._analysis_path(owner, analysis_id)
            rec = json.loads(path.read_text(encoding="utf-8"))
            rec["history"].extend(messages)
            path.write_text(json.dumps(rec), encoding="utf-8")


# --------------------------------------------------------------------------
# Azure Blob Storage
# --------------------------------------------------------------------------


class BlobStore(_MemoryAnalyses):
    def __init__(self, account_url: str, reference_container: str, upload_container: str,
                 credential) -> None:
        super().__init__()
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(account_url=account_url, credential=credential)
        self.refs = service.get_container_client(reference_container)
        self.uploads = service.get_container_client(upload_container)

    def save_reference(self, ref: ReferenceTyre, jpeg: bytes) -> None:
        from azure.storage.blob import ContentSettings

        rid = check_id(ref.id)
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
            return self.refs.download_blob(f"{check_id(ref_id)}.jpg").readall()
        except ResourceNotFoundError as exc:
            raise KeyError(ref_id) from exc

    def delete_reference(self, ref_id: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        rid = check_id(ref_id)
        for name in (f"{rid}.jpg", f"{rid}.json"):
            try:
                self.refs.delete_blob(name)
            except ResourceNotFoundError:
                pass

    def save_upload(self, owner: str, upload_id: str, jpeg: bytes) -> None:
        from azure.storage.blob import ContentSettings

        name = f"{check_owner(owner)}/{check_id(upload_id)}.jpg"
        self.uploads.upload_blob(name, jpeg, overwrite=False,
                                 content_settings=ContentSettings(content_type="image/jpeg"))


def build_data_store(settings: Settings, credential) -> DataStore:
    if settings.data_backend == "azure_blob":
        return BlobStore(settings.storage_account_url, settings.reference_container,
                         settings.upload_container, credential)
    if settings.data_backend == "sqlserver":
        from .sqlserver_store import SqlServerStore

        store = SqlServerStore(settings.sqlserver_server, settings.sqlserver_database,
                               trust_server_certificate=settings.sqlserver_trust_server_certificate)
        store.initialize()  # creates the database and tables if missing
        return store
    return LocalFileStore(settings.local_data_dir)
