"""RouteLens against litellm.Router directly (no proxy), using mock responses. Needs no network or keys."""
import asyncio

import litellm
import pytest
from litellm import Router

from routelens.callback import RouteLens


@pytest.fixture()
def lens(tmp_path):
    saved = litellm.callbacks
    lens = RouteLens(db_path=str(tmp_path / "sdk.db"), capture_content="preview", mount=False)
    litellm.callbacks = [lens]
    yield lens
    litellm.callbacks = saved
    lens.store.close()


def make_router(lens):
    r = Router(
        model_list=[
            {"model_name": "fast", "litellm_params": {"model": "openai/fast-a", "api_key": "x", "mock_response": "a"}, "model_info": {"id": "dep-a"}},
            {"model_name": "fast", "litellm_params": {"model": "anthropic/fast-b", "api_key": "x", "mock_response": "b"}, "model_info": {"id": "dep-b"}},
            {"model_name": "flaky", "litellm_params": {"model": "openai/flaky", "api_key": "x", "mock_response": "litellm.RateLimitError"}, "model_info": {"id": "dep-flaky"}},
            {"model_name": "backup", "litellm_params": {"model": "openai/backup", "api_key": "x", "mock_response": "ok"}, "model_info": {"id": "dep-backup"}},
        ],
        fallbacks=[{"flaky": ["backup"]}], num_retries=0, routing_strategy="latency-based-routing")
    lens.attach(r)
    return r


async def settle(lens):
    await asyncio.sleep(0.6)  # LiteLLM logs success/failure in background tasks
    lens.store.flush()


def test_records_candidates_winner_and_reason(lens):
    async def go():
        r = make_router(lens)
        await r.acompletion(model="fast", messages=[{"role": "user", "content": "hello there"}])
        await settle(lens)
    asyncio.run(go())
    [a] = lens.store.recent()
    assert a["status"] == "success" and a["model_group"] == "fast"
    assert {c["id"] for c in a["candidates"]} == {"dep-a", "dep-b"}
    assert a["deployment_id"] in {"dep-a", "dep-b"} and a["reason_kind"] == "latency"
    assert a["preview"] == "hello there"


def test_fallback_is_one_request_with_two_attempts(lens):
    async def go():
        r = make_router(lens)
        await r.acompletion(model="flaky", messages=[{"role": "user", "content": "will fail over"}])
        await settle(lens)
    asyncio.run(go())
    rows = sorted(lens.store.recent(), key=lambda a: a["ts"])
    assert [(a["model_group"], a["status"], a["attempt_no"]) for a in rows] == [("flaky", "failure", 0), ("backup", "success", 1)]
    assert len({a["request_id"] for a in rows}) == 1
    assert rows[1]["reason_kind"] == "fallback" and "RateLimitError" in rows[1]["reason"]
    assert rows[0]["error_class"] == "RateLimitError"


def test_conversation_is_grouped_without_a_session_id(lens):
    async def go():
        r = make_router(lens)
        first = {"role": "user", "content": "plan my trip to Lisbon"}
        await r.acompletion(model="fast", messages=[first])
        await r.acompletion(model="fast", messages=[first, {"role": "assistant", "content": "sure"}, {"role": "user", "content": "add day two"}])
        await r.acompletion(model="fast", messages=[{"role": "user", "content": "an unrelated chat"}])
        await settle(lens)
    asyncio.run(go())
    sessions = lens.store.sessions(0)
    assert sorted(s["turns"] for s in sessions) == [1, 2]
    assert all(s["session_source"] == "inferred" for s in sessions)


def test_explicit_session_id_wins_and_turns_stay_separate(lens):
    async def go():
        r = make_router(lens)
        for text in ("one", "two", "three"):
            await r.acompletion(model="fast", messages=[{"role": "user", "content": text}], metadata={"session_id": "S-1"},
                                litellm_trace_id="SHARED-TRACE")
        await settle(lens)
    asyncio.run(go())
    [s] = lens.store.sessions(0)
    assert s["session_id"] == "S-1" and s["session_source"] == "explicit"
    # a shared trace id (what x-litellm-session-id produces in the proxy) must not merge separate turns
    assert s["turns"] == 3


def test_output_moderation_blocks_a_flagged_response_end_to_end(tmp_path):
    """async_post_call_success_deployment_hook (unlike pre_call_hook) fires for a bare SDK
    Router too, so this is the one moderation path testable without a real proxy process."""
    from routelens.moderation import ModerationResult

    class FlagsWord(object):
        enabled = True

        async def check(self, text):
            return ModerationResult(flagged=("BLOCKED_WORD" in text))

    saved = litellm.callbacks
    m = RouteLens(db_path=str(tmp_path / "out_mod.db"), mount=False,
                  moderation_enabled=True, moderation_checker=FlagsWord())
    litellm.callbacks = [m]
    r = Router(model_list=[
        {"model_name": "chat", "litellm_params": {"model": "openai/ok", "api_key": "x", "mock_response": "clean reply"}},
        {"model_name": "flagged", "litellm_params": {"model": "openai/bad", "api_key": "x", "mock_response": "contains BLOCKED_WORD here"}},
    ])

    async def go():
        clean = await r.acompletion(model="chat", messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(litellm.BadRequestError):
            await r.acompletion(model="flagged", messages=[{"role": "user", "content": "hi"}])
        return clean
    try:
        clean = asyncio.run(go())
        assert clean.choices[0].message.content == "clean reply"
        m.store.flush()
        statuses = sorted(e["status"] for e in m.store.moderation_recent())
        assert statuses == ["blocked", "pass"]
    finally:
        litellm.callbacks = saved
        m.store.close()
