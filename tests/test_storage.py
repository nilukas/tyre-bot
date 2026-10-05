"""Storage backends behave the same. The SQL Server test runs only when TEST_SQLSERVER=1."""

import datetime as dt
import os

import pytest

from app.analysis import MockAdvisor
from app.schemas import ReferenceCondition, ReferenceTyre, VehicleType
from app.storage import LocalFileStore, new_id

OWNER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


def _ref() -> ReferenceTyre:
    return ReferenceTyre(id=new_id(), condition=ReferenceCondition.worn, tread_depth_mm=3.2,
                         km_driven=40000, created_by="Test",
                         created_at=dt.datetime.now(dt.timezone.utc).isoformat())


def _exercise(store):
    ref = _ref()
    store.save_reference(ref, b"\xff\xd8jpeg")
    assert any(r.id == ref.id for r in store.list_references())
    assert store.get_reference_image(ref.id) == b"\xff\xd8jpeg"

    aid = new_id()
    store.save_upload(OWNER, aid, b"\xff\xd8customer")
    advisor = MockAdvisor()
    assessment = advisor.assess(store, b"\xff\xd8customer", VehicleType.car)
    estimate = {"verdict": "ok", "min_tread_mm": 6.0, "km_to_legal_limit": 35000}
    store.save_analysis(OWNER, aid, "car", assessment.model_dump(mode="json"), estimate)
    store.append_messages(OWNER, aid, [{"role": "user", "content": "How far?"},
                                       {"role": "assistant", "content": "About 35,000 km."}])

    rec = store.get_analysis(OWNER, aid)
    assert rec["estimate"]["km_to_legal_limit"] == 35000
    assert [m["role"] for m in rec["history"]] == ["user", "assistant"]
    # Another customer can never read it
    assert store.get_analysis(OTHER, aid) is None

    store.delete_reference(ref.id)
    with pytest.raises(KeyError):
        store.get_reference_image(ref.id)


def test_local_file_store(tmp_path):
    _exercise(LocalFileStore(str(tmp_path)))


@pytest.mark.skipif(os.environ.get("TEST_SQLSERVER") != "1", reason="set TEST_SQLSERVER=1 to run")
def test_sqlserver_store():
    from app.sqlserver_store import SqlServerStore

    store = SqlServerStore(os.environ.get("SQLSERVER_SERVER", r"localhost\SQLEXPRESS"),
                           os.environ.get("SQLSERVER_TEST_DATABASE", "TyreAdvisorTest"),
                           trust_server_certificate=True)
    store.initialize()
    store.initialize()  # idempotent
    _exercise(store)


def test_mock_advisor_is_repeatable(tmp_path):
    a = MockAdvisor()
    store = LocalFileStore(str(tmp_path))
    first = a.assess(store, b"photo-1", VehicleType.car)
    assert a.assess(store, b"photo-1", VehicleType.car) == first
    assert "MOCK" in first.observations
