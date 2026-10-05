"""Local SQL Server backend (DATA_BACKEND=sqlserver, development only).

Everything lives in one database: reference photos + labels, customer photos,
analyses (JSON plus a few columns for easy reporting) and chat history.

On startup ``initialize()`` creates the database, tables and indexes if they don't
exist, so a fresh machine only needs SQL Server installed and running.

Auth is Windows Integrated (Trusted_Connection): no SQL logins or passwords.
Driver: Microsoft's official ``mssql-python``. All values are passed as ``?`` parameters.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager

import mssql_python

from .schemas import ReferenceTyre
from .storage import check_id, check_owner

_DB_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SERVER_RE = re.compile(r"^[A-Za-z0-9_.,:\\()\-]{1,255}$")

SCHEMA = [
    """
    IF OBJECT_ID(N'dbo.reference_tyres', N'U') IS NULL
    CREATE TABLE dbo.reference_tyres (
        id              CHAR(32)       NOT NULL CONSTRAINT PK_reference_tyres PRIMARY KEY,
        condition       VARCHAR(20)    NOT NULL,
        tread_depth_mm  FLOAT          NOT NULL,
        km_driven       INT            NULL,
        tyre_size       NVARCHAR(40)   NULL,
        brand_model     NVARCHAR(80)   NULL,
        notes           NVARCHAR(500)  NULL,
        created_by      NVARCHAR(200)  NOT NULL,
        created_at      DATETIME2(0)   NOT NULL,
        image           VARBINARY(MAX) NOT NULL
    )
    """,
    """
    IF OBJECT_ID(N'dbo.customer_uploads', N'U') IS NULL
    CREATE TABLE dbo.customer_uploads (
        id          CHAR(32)       NOT NULL CONSTRAINT PK_customer_uploads PRIMARY KEY,
        owner_id    NVARCHAR(64)   NOT NULL,
        image       VARBINARY(MAX) NOT NULL,
        created_at  DATETIME2(0)   NOT NULL CONSTRAINT DF_customer_uploads_created DEFAULT SYSUTCDATETIME()
    )
    """,
    """
    IF OBJECT_ID(N'dbo.analyses', N'U') IS NULL
    CREATE TABLE dbo.analyses (
        id                 CHAR(32)      NOT NULL CONSTRAINT PK_analyses PRIMARY KEY,
        owner_id           NVARCHAR(64)  NOT NULL,
        upload_id          CHAR(32)      NULL CONSTRAINT FK_analyses_upload REFERENCES dbo.customer_uploads(id),
        vehicle_type       VARCHAR(20)   NOT NULL,
        verdict            VARCHAR(20)   NOT NULL,
        min_tread_mm       FLOAT         NOT NULL,
        km_to_legal_limit  INT           NOT NULL,
        confidence         VARCHAR(10)   NOT NULL,
        assessment_json    NVARCHAR(MAX) NOT NULL CONSTRAINT CK_analyses_assessment CHECK (ISJSON(assessment_json) = 1),
        estimate_json      NVARCHAR(MAX) NOT NULL CONSTRAINT CK_analyses_estimate CHECK (ISJSON(estimate_json) = 1),
        created_at         DATETIME2(0)  NOT NULL CONSTRAINT DF_analyses_created DEFAULT SYSUTCDATETIME()
    )
    """,
    """
    IF OBJECT_ID(N'dbo.chat_messages', N'U') IS NULL
    CREATE TABLE dbo.chat_messages (
        id           BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_chat_messages PRIMARY KEY,
        analysis_id  CHAR(32)      NOT NULL CONSTRAINT FK_chat_analysis REFERENCES dbo.analyses(id) ON DELETE CASCADE,
        role         VARCHAR(10)   NOT NULL CONSTRAINT CK_chat_role CHECK (role IN ('user', 'assistant')),
        content      NVARCHAR(MAX) NOT NULL,
        created_at   DATETIME2(0)  NOT NULL CONSTRAINT DF_chat_created DEFAULT SYSUTCDATETIME()
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_analyses_owner')
    CREATE INDEX IX_analyses_owner ON dbo.analyses(owner_id, created_at DESC)
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_uploads_owner')
    CREATE INDEX IX_uploads_owner ON dbo.customer_uploads(owner_id)
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_chat_analysis')
    CREATE INDEX IX_chat_analysis ON dbo.chat_messages(analysis_id, id)
    """,
]


def _to_utc_naive(iso: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(iso)
    if value.tzinfo:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def _to_iso(value: dt.datetime) -> str:
    return value.replace(tzinfo=dt.timezone.utc).isoformat()


class SqlServerStore:
    def __init__(self, server: str, database: str, *, trust_server_certificate: bool) -> None:
        if not _SERVER_RE.match(server):
            raise RuntimeError("SQLSERVER_SERVER contains invalid characters")
        if not _DB_NAME_RE.match(database):
            raise RuntimeError("SQLSERVER_DATABASE must be letters, digits or underscores")
        self.server = server
        self.database = database
        # TrustServerCertificate=yes is acceptable only for a local dev instance with a
        # self-signed certificate. The connection is still encrypted.
        self._trust = "yes" if trust_server_certificate else "no"

    def _conn_str(self, database: str) -> str:
        return (f"Server={self.server};Database={database};Trusted_Connection=yes;"
                f"Encrypt=yes;TrustServerCertificate={self._trust}")

    @contextmanager
    def _connect(self, database: str | None = None, *, autocommit: bool = False) -> Iterator:
        conn = mssql_python.connect(self._conn_str(database or self.database), autocommit=autocommit)
        try:
            yield conn
            if not autocommit:
                conn.commit()
        except Exception:
            if not autocommit:
                conn.rollback()
            raise
        finally:
            conn.close()

    # ---- setup -------------------------------------------------------------

    def initialize(self) -> None:
        try:
            with self._connect("master", autocommit=True) as conn:
                # Name is validated by _DB_NAME_RE; DDL cannot take parameters.
                conn.cursor().execute(
                    f"IF DB_ID(N'{self.database}') IS NULL CREATE DATABASE [{self.database}]")
            with self._connect() as conn:
                cur = conn.cursor()
                for statement in SCHEMA:
                    cur.execute(statement)
        except mssql_python.Error as exc:
            raise RuntimeError(
                f"Could not set up SQL Server database '{self.database}' on '{self.server}'. "
                "Check that SQL Server is running and your Windows account can create databases."
            ) from exc

    # ---- reference library -------------------------------------------------

    def save_reference(self, ref: ReferenceTyre, jpeg: bytes) -> None:
        with self._connect() as conn:
            conn.cursor().execute(
                "INSERT INTO dbo.reference_tyres (id, condition, tread_depth_mm, km_driven, tyre_size,"
                " brand_model, notes, created_by, created_at, image) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (check_id(ref.id), ref.condition.value, ref.tread_depth_mm, ref.km_driven,
                 ref.tyre_size, ref.brand_model, ref.notes, ref.created_by,
                 _to_utc_naive(ref.created_at), jpeg),
            )

    def list_references(self) -> list[ReferenceTyre]:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, condition, tread_depth_mm, km_driven, tyre_size, brand_model, notes,"
                " created_by, created_at FROM dbo.reference_tyres ORDER BY created_at DESC")
            rows = cur.fetchall()
        return [
            ReferenceTyre(id=r[0], condition=r[1], tread_depth_mm=r[2], km_driven=r[3],
                          tyre_size=r[4], brand_model=r[5], notes=r[6], created_by=r[7],
                          created_at=_to_iso(r[8]))
            for r in rows
        ]

    def get_reference_image(self, ref_id: str) -> bytes:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT image FROM dbo.reference_tyres WHERE id = ?", (check_id(ref_id),))
            row = cur.fetchone()
        if row is None:
            raise KeyError(ref_id)
        return bytes(row[0])

    def delete_reference(self, ref_id: str) -> None:
        with self._connect() as conn:
            conn.cursor().execute("DELETE FROM dbo.reference_tyres WHERE id = ?", (check_id(ref_id),))

    # ---- customer data -----------------------------------------------------

    def save_upload(self, owner: str, upload_id: str, jpeg: bytes) -> None:
        with self._connect() as conn:
            conn.cursor().execute(
                "INSERT INTO dbo.customer_uploads (id, owner_id, image) VALUES (?, ?, ?)",
                (check_id(upload_id), check_owner(owner), jpeg))

    def save_analysis(self, owner: str, analysis_id: str, vehicle_type: str,
                      assessment: dict, estimate: dict) -> None:
        # The customer photo for an analysis is saved first under the same id.
        aid = check_id(analysis_id)
        with self._connect() as conn:
            conn.cursor().execute(
                "INSERT INTO dbo.analyses (id, owner_id, upload_id, vehicle_type, verdict, min_tread_mm,"
                " km_to_legal_limit, confidence, assessment_json, estimate_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (aid, check_owner(owner), aid, vehicle_type, estimate["verdict"],
                 estimate["min_tread_mm"], estimate["km_to_legal_limit"], assessment["confidence"],
                 json.dumps(assessment), json.dumps(estimate)),
            )

    def get_analysis(self, owner: str, analysis_id: str) -> dict | None:
        try:
            aid, oid = check_id(analysis_id), check_owner(owner)
        except KeyError:
            return None
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT assessment_json, estimate_json FROM dbo.analyses"
                        " WHERE id = ? AND owner_id = ?", (aid, oid))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("SELECT role, content FROM dbo.chat_messages WHERE analysis_id = ?"
                        " ORDER BY id", (aid,))
            history = [{"role": r[0], "content": r[1]} for r in cur.fetchall()]
        return {"assessment": json.loads(row[0]), "estimate": json.loads(row[1]), "history": history}

    def append_messages(self, owner: str, analysis_id: str, messages: list[dict]) -> None:
        aid, oid = check_id(analysis_id), check_owner(owner)
        with self._connect() as conn:
            cur = conn.cursor()
            for m in messages:
                # Ownership enforced in the INSERT itself.
                cur.execute(
                    "INSERT INTO dbo.chat_messages (analysis_id, role, content)"
                    " SELECT id, ?, ? FROM dbo.analyses WHERE id = ? AND owner_id = ?",
                    (m["role"], m["content"], aid, oid))
