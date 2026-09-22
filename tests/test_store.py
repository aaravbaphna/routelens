import time
import uuid

import pytest

from routelens.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def row(session, request, ts, model="m1", provider="openai", status="success", attempt_no=0, **kw):
    r = {"call_id": uuid.uuid4().hex, "request_id": request, "session_id": session, "session_source": "explicit",
         "ts": ts, "attempt_no": attempt_no, "model_group": "g", "deployment_id": model, "model": model,
         "provider": provider, "status": status, "latency_ms": 100.0, "cost": 0.01, "reason_kind": "shuffle",
         "reason": "r", "candidates": [{"id": model}], "excluded": [], "tags": []}
    r.update(kw)
    return r


def test_turns_group_by_request_and_count_switches(store):
    now = time.time()
    store.record(row("s1", "r1", now - 30, model="a"))
    store.record(row("s1", "r2", now - 20, model="b", provider="anthropic"))
    store.record(row("s1", "r3", now - 10, model="b", provider="anthropic"))
    store.flush()
    [s] = store.sessions(now - 3600)
    assert s["turns"] == 3 and s["switches"] == 1 and [p["model"] for p in s["path"]] == ["a", "b", "b"]


def test_fallback_is_one_turn_with_two_attempts(store):
    now = time.time()
    store.record(row("s1", "r1", now - 5, model="flaky", status="failure", error_class="RateLimitError"))
    store.record(row("s1", "r1", now - 4, model="backup", attempt_no=1))
    store.flush()
    d = store.session("s1")
    [turn] = d["turns"]
    assert turn["status"] == "success" and turn["final"]["model"] == "backup" and len(turn["attempts"]) == 2
    [s] = store.sessions(now - 3600)
    assert s["rerouted"] == 1 and s["failed_attempts"] == 1 and s["path"][0]["model"] == "backup"


def test_overview_totals_and_percentiles(store):
    now = time.time()
    for i in range(10):
        store.record(row("s%d" % i, "r%d" % i, now - i, latency_ms=100.0 * (i + 1)))
    store.flush()
    o = store.overview(now - 3600, 60)
    assert o["totals"]["requests"] == 10 and o["totals"]["sessions"] == 10
    assert o["totals"]["p50_ms"] == pytest.approx(500.0) and o["totals"]["p95_ms"] == pytest.approx(1000.0)
    assert o["by_model"][0]["n"] >= 1 and sum(b["n"] for b in o["series"]) == 10


def test_provider_order_is_first_seen(store):
    now = time.time()
    store.record(row("s", "r1", now - 10, provider="anthropic"))
    store.record(row("s", "r2", now - 5, provider="openai"))
    store.flush()
    assert store.providers() == ["anthropic", "openai"]


def test_search_and_window(store):
    now = time.time()
    store.record(row("old", "r1", now - 10 * 86400))
    store.record(row("new-convo", "r2", now - 5, preview="refund policy"))
    store.flush()
    assert [s["session_id"] for s in store.sessions(now - 86400)] == ["new-convo"]
    assert store.sessions(now - 86400, q="refund")[0]["session_id"] == "new-convo"
    assert store.sessions(now - 86400, q="nomatch") == []


def test_retention_prunes_old_rows(tmp_path):
    s = Store(str(tmp_path / "r.db"), retention_days=1)
    s.record(row("a", "r1", time.time() - 5 * 86400))
    s.record(row("b", "r2", time.time()))
    s.flush()
    assert [x["session_id"] for x in s.sessions(0)] == ["b"]
    s.close()
