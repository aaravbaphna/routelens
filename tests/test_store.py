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


def mod_row(session, request, ts, chain="input", status="pass", categories=None, key_alias="k", **kw):
    r = {"id": uuid.uuid4().hex, "request_id": request, "session_id": session, "session_source": "explicit",
         "ts": ts, "chain": chain, "status": status, "categories": categories or [], "scores": {},
         "reason": "r", "latency_ms": 50.0, "preview": None, "key_alias": key_alias}
    r.update(kw)
    return r


def test_blocked_at_input_turn_has_no_attempts_row_but_still_shows_up(store):
    now = time.time()
    store.record_moderation(mod_row("s1", "r1", now, chain="input", status="blocked",
                                     categories=["violence"], preview="bad prompt"))
    store.flush()
    d = store.session("s1")
    [turn] = d["turns"]
    assert turn["status"] == "blocked" and turn["final"] is None and turn["attempts"] == []
    assert turn["preview"] == "bad prompt"
    [s] = store.sessions(now - 3600)
    assert s["turns"] == 1 and s["blocked"] == 1
    assert s["path"] == [{"provider": None, "model": None, "status": "blocked"}]


def test_output_blocked_turn_keeps_its_successful_attempt_but_is_marked_blocked(store):
    now = time.time()
    store.record(row("s1", "r1", now - 1, model="a"))
    store.record_moderation(mod_row("s1", "r1", now, chain="output", status="blocked", categories=["sexual"]))
    store.flush()
    d = store.session("s1")
    [turn] = d["turns"]
    assert turn["status"] == "blocked" and turn["final"]["model"] == "a"  # the attempt itself still succeeded
    [s] = store.sessions(now - 3600)
    assert s["path"][0]["status"] == "blocked" and s["path"][0]["model"] == "a"


def test_a_passing_moderation_check_does_not_mark_the_turn_blocked(store):
    now = time.time()
    store.record(row("s1", "r1", now - 1, model="a"))
    store.record_moderation(mod_row("s1", "r1", now, chain="input", status="pass"))
    store.record_moderation(mod_row("s1", "r1", now, chain="output", status="pass"))
    store.flush()
    d = store.session("s1")
    [turn] = d["turns"]
    assert turn["status"] == "success"


def test_mixed_session_orders_turns_by_time_across_both_tables(store):
    now = time.time()
    store.record_moderation(mod_row("s1", "r1", now - 20, chain="input", status="blocked"))
    store.record(row("s1", "r2", now - 10, model="a"))
    store.record_moderation(mod_row("s1", "r3", now, chain="input", status="blocked"))
    store.flush()
    d = store.session("s1")
    assert [t["request_id"] for t in d["turns"]] == ["r1", "r2", "r3"]
    assert [t["turn"] for t in d["turns"]] == [1, 2, 3]
    [s] = store.sessions(now - 3600)
    assert s["turns"] == 3 and s["blocked"] == 2
    assert [p["status"] for p in s["path"]] == ["blocked", "success", "blocked"]


def test_session_source_falls_back_to_moderation_when_there_are_no_attempts(store):
    now = time.time()
    store.record_moderation(mod_row("s1", "r1", now, chain="input", status="blocked"))
    store.flush()
    d = store.session("s1")
    assert d["session_source"] == "explicit"


def test_moderation_overview_totals_and_category_breakdown(store):
    now = time.time()
    store.record_moderation(mod_row("s1", "r1", now, chain="input", status="pass"))
    store.record_moderation(mod_row("s1", "r2", now, chain="input", status="blocked", categories=["violence"]))
    store.record_moderation(mod_row("s1", "r2", now, chain="output", status="blocked", categories=["violence", "sexual"]))
    store.record_moderation(mod_row("s1", "r3", now, chain="output", status="error"))
    store.flush()
    o = store.moderation_overview(now - 3600, 60)
    assert o["totals"]["checked"] == 4 and o["totals"]["blocked"] == 2 and o["totals"]["errors"] == 1
    assert o["totals"]["block_rate"] == pytest.approx(0.5)
    by_chain = {c["chain"]: c for c in o["by_chain"]}
    assert by_chain["input"]["checked"] == 2 and by_chain["input"]["blocked"] == 1
    assert by_chain["output"]["checked"] == 2 and by_chain["output"]["blocked"] == 1
    cats = {c["category"]: c["n"] for c in o["by_category"]}
    assert cats == {"violence": 2, "sexual": 1}


def test_moderation_overview_filters_by_chain(store):
    now = time.time()
    store.record_moderation(mod_row("s1", "r1", now, chain="input", status="blocked", categories=["a"]))
    store.record_moderation(mod_row("s1", "r2", now, chain="output", status="pass"))
    store.flush()
    o = store.moderation_overview(now - 3600, 60, chain="input")
    assert o["totals"]["checked"] == 1 and o["totals"]["blocked"] == 1


def test_moderation_recent_blocked_only(store):
    now = time.time()
    store.record_moderation(mod_row("s1", "r1", now - 2, status="pass"))
    store.record_moderation(mod_row("s1", "r2", now - 1, status="blocked"))
    store.flush()
    recent = store.moderation_recent(blocked_only=True)
    assert [r["request_id"] for r in recent] == ["r2"]


def test_moderation_for_requests_groups_by_request_id(store):
    now = time.time()
    store.record_moderation(mod_row("s1", "r1", now, chain="input", status="pass"))
    store.record_moderation(mod_row("s1", "r1", now, chain="output", status="blocked"))
    store.record_moderation(mod_row("s1", "r2", now, chain="input", status="pass"))
    store.flush()
    grouped = store.moderation_for_requests(["r1", "r2", "r9"])
    assert len(grouped["r1"]) == 2 and len(grouped["r2"]) == 1 and "r9" not in grouped
