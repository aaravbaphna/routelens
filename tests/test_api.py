"""The FastAPI surface, exercised directly with a TestClient -- no litellm proxy needed, since
`build_router()` returns a plain APIRouter you can mount anywhere."""
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routelens.api import build_router
from routelens.store import Store


def client(store, **kw):
    app = FastAPI()
    app.include_router(build_router(store, **kw))
    return TestClient(app)


def test_meta_reports_moderation_disabled_by_default(tmp_path):
    c = client(Store(str(tmp_path / "t.db")))
    body = c.get("/routelens/api/meta").json()
    assert body["moderation"] == {"enabled": False, "mode": "enforce"}


def test_meta_reports_moderation_when_enabled(tmp_path):
    c = client(Store(str(tmp_path / "t.db")), moderation_enabled=True, moderation_mode="observe")
    body = c.get("/routelens/api/meta").json()
    assert body["moderation"] == {"enabled": True, "mode": "observe"}


def test_moderation_overview_and_recent_end_to_end(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    now = time.time()
    store.record_moderation({"id": "1", "request_id": "r1", "session_id": "s1", "session_source": "explicit",
                              "ts": now, "chain": "input", "status": "blocked", "categories": ["violence"],
                              "scores": {}, "reason": "flagged for violence", "latency_ms": 10.0,
                              "preview": None, "key_alias": None})
    store.record_moderation({"id": "2", "request_id": "r2", "session_id": "s1", "session_source": "explicit",
                              "ts": now, "chain": "output", "status": "pass", "categories": [], "scores": {},
                              "reason": None, "latency_ms": 5.0, "preview": None, "key_alias": None})
    store.flush()
    c = client(store)

    overview = c.get("/routelens/api/moderation/overview?window=24h").json()
    assert overview["totals"]["checked"] == 2 and overview["totals"]["blocked"] == 1
    assert overview["by_category"] == [{"category": "violence", "n": 1}]

    input_only = c.get("/routelens/api/moderation/overview?window=24h&chain=input").json()
    assert input_only["totals"]["checked"] == 1 and input_only["chain"] == "input"

    recent = c.get("/routelens/api/moderation/recent").json()["events"]
    assert len(recent) == 2

    blocked = c.get("/routelens/api/moderation/recent?blocked_only=true").json()["events"]
    assert [e["request_id"] for e in blocked] == ["r1"]

    store.close()


def test_session_endpoint_includes_blocked_turn_with_no_attempts(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    now = time.time()
    store.record_moderation({"id": "1", "request_id": "r1", "session_id": "s1", "session_source": "explicit",
                              "ts": now, "chain": "input", "status": "blocked", "categories": ["sexual"],
                              "scores": {}, "reason": "flagged for sexual", "latency_ms": 8.0,
                              "preview": "bad prompt", "key_alias": None})
    store.flush()
    c = client(store)
    body = c.get("/routelens/api/sessions/s1").json()
    [turn] = body["turns"]
    assert turn["status"] == "blocked" and turn["final"] is None and turn["preview"] == "bad prompt"
    store.close()
