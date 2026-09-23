"""RouteLens's moderation logic in isolation: a fake checker, no network, no litellm proxy.

`async_pre_call_hook` (input moderation) only fires inside a real litellm *proxy* process --
verified empirically, not assumed; see the README and the real-proxy smoke test in
test_callback_sdk.py for that half. Everything here calls `_check_moderation` directly, which is
the exact same code both hooks call into, so it's the right place to cover the enforce/observe/
fail-open decision logic thoroughly without needing a proxy at all.
"""
import asyncio

import litellm
import pytest

from routelens.callback import RouteLens
from routelens.moderation import ModerationResult


class FakeChecker:
    """Same shape as ModerationChecker, pre-programmed instead of calling a real API."""

    def __init__(self, result: ModerationResult):
        self.enabled = True
        self.result = result
        self.calls = []

    async def check(self, text: str) -> ModerationResult:
        self.calls.append(text)
        return self.result


def make_lens(tmp_path, result, **kw):
    checker = FakeChecker(result)
    lens = RouteLens(db_path=str(tmp_path / "m.db"), mount=False, moderation_enabled=True,
                      moderation_checker=checker, **kw)
    return lens, checker


def run(coro):
    return asyncio.run(coro)


def test_passing_content_is_recorded_and_not_blocked(tmp_path):
    lens, checker = make_lens(tmp_path, ModerationResult(flagged=False))
    run(lens._check_moderation("input", {"metadata": {"routelens_request_id": "r1"}}, "hello there"))
    lens.store.flush()
    [event] = lens.store.moderation_recent()
    assert event["status"] == "pass" and event["chain"] == "input" and event["request_id"] == "r1"


def test_flagged_content_is_blocked_in_enforce_mode(tmp_path):
    lens, checker = make_lens(tmp_path, ModerationResult(flagged=True, categories=["violence"]))
    with pytest.raises(litellm.BadRequestError, match="violence"):
        run(lens._check_moderation("input", {"metadata": {"routelens_request_id": "r1"}}, "bad text"))
    lens.store.flush()
    [event] = lens.store.moderation_recent()
    assert event["status"] == "blocked" and event["categories"] == ["violence"]


def test_flagged_content_is_recorded_but_not_blocked_in_observe_mode(tmp_path):
    lens, checker = make_lens(tmp_path, ModerationResult(flagged=True, categories=["violence"]),
                               moderation_mode="observe")
    run(lens._check_moderation("output", {"metadata": {"routelens_request_id": "r1"}}, "bad text"))  # must not raise
    lens.store.flush()
    [event] = lens.store.moderation_recent()
    assert event["status"] == "blocked" and event["chain"] == "output"  # verdict still recorded truthfully


def test_empty_text_is_skipped_entirely(tmp_path):
    lens, checker = make_lens(tmp_path, ModerationResult(flagged=True))  # would block if it ran
    run(lens._check_moderation("input", {"metadata": {}}, "   "))
    lens.store.flush()
    assert lens.store.moderation_recent() == [] and checker.calls == []


def test_api_error_fails_open_by_default(tmp_path):
    lens, checker = make_lens(tmp_path, ModerationResult(error="ConnectError: refused"))
    run(lens._check_moderation("input", {"metadata": {"routelens_request_id": "r1"}}, "hi"))  # must not raise
    lens.store.flush()
    [event] = lens.store.moderation_recent()
    assert event["status"] == "error"


def test_api_error_blocks_when_fail_open_is_off(tmp_path):
    lens, checker = make_lens(tmp_path, ModerationResult(error="ConnectError: refused"),
                               moderation_fail_open=False)
    with pytest.raises(litellm.BadRequestError, match="fail_open is off"):
        run(lens._check_moderation("input", {"metadata": {"routelens_request_id": "r1"}}, "hi"))
    lens.store.flush()
    [event] = lens.store.moderation_recent()
    assert event["status"] == "error"


def test_moderation_is_off_by_default(tmp_path):
    lens = RouteLens(db_path=str(tmp_path / "m.db"), mount=False)
    assert lens._moderation is None


def test_moderation_flag_alone_does_not_enable_without_an_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ROUTELENS_MODERATION_API_KEY", raising=False)
    lens = RouteLens(db_path=str(tmp_path / "m.db"), mount=False, moderation_enabled=True)
    assert lens._moderation is None  # warned and disabled itself, rather than making doomed API calls


def test_invalid_mode_falls_back_to_enforce(tmp_path):
    lens, _ = make_lens(tmp_path, ModerationResult(flagged=False), moderation_mode="sideways")
    assert lens.moderation_mode == "enforce"


def test_input_and_output_use_different_chain_labels(tmp_path):
    lens, checker = make_lens(tmp_path, ModerationResult(flagged=False))
    run(lens._check_moderation("input", {"metadata": {"routelens_request_id": "r1"}}, "a"))
    run(lens._check_moderation("output", {"metadata": {"routelens_request_id": "r1"}}, "b"))
    lens.store.flush()
    chains = sorted(e["chain"] for e in lens.store.moderation_recent())
    assert chains == ["input", "output"]
