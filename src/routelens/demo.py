"""Synthetic multi-turn sessions so you can explore the dashboard without any API keys.

    routelens demo --db demo.db --serve
"""
from __future__ import annotations

import random
import time
import uuid
from typing import Any, Dict, List, Optional

from .explain import EXCLUDED_GENERIC, explain
from .store import Store

D = {  # deployment id -> (provider, model, $/1k prompt tok, $/1k completion tok, typical latency ms)
    "oai-mini": ("openai", "gpt-4o-mini", 0.00015, 0.0006, 700),
    "oai-o3": ("openai", "o3", 0.002, 0.008, 5200),
    "ant-sonnet": ("anthropic", "claude-sonnet-4", 0.003, 0.015, 2400),
    "ant-haiku": ("anthropic", "claude-haiku-4-5", 0.0008, 0.004, 900),
    "gem-flash": ("gemini", "gemini-2.5-flash", 0.0003, 0.0025, 1100),
    "bed-llama": ("bedrock", "llama-3.3-70b", 0.00072, 0.00072, 1600),
}


def _cand(ids: List[str]) -> List[Dict[str, Any]]:
    return [{"id": i, "model": D[i][1], "provider": D[i][0], "weight": None} for i in ids]


def _row(rng: random.Random, *, session: str, trace: str, ts: float, attempt_no: int, group: str,
         requested: str, strategy: str, dep: str, candidates: List[str], all_ids: List[str],
         ok: bool = True, prev_failure: Optional[dict] = None, complexity: Optional[dict] = None,
         preview: str = "", error: Optional[tuple] = None, prompt_tok: int = 400, out_tok: int = 250) -> Dict[str, Any]:
    provider, model, pc, cc, lat = D[dep]
    cands = _cand(candidates)
    excluded = [dict(c, why=EXCLUDED_GENERIC) for c in _cand([i for i in all_ids if i not in candidates])]
    kind, headline, details = explain(
        strategy=strategy, group=group, requested_model=requested, candidates=cands, excluded=excluded,
        chosen_id=dep, prev_failure=prev_failure, complexity=complexity, tags=[])
    latency = max(120.0, rng.gauss(lat, lat * 0.18))
    return {
        "call_id": uuid.uuid4().hex, "request_id": trace, "session_id": session, "session_source": "explicit",
        "ts": ts, "attempt_no": attempt_no, "requested_model": requested, "model_group": group,
        "deployment_id": dep, "model": model, "provider": provider,
        "status": "success" if ok else "failure", "latency_ms": latency if ok else rng.uniform(150, 400),
        "cost": (prompt_tok * pc + out_tok * cc) / 1000 if ok else 0.0,
        "prompt_tokens": prompt_tok if ok else 0, "completion_tokens": out_tok if ok else 0, "cache_hit": 0,
        "strategy": strategy, "reason_kind": kind, "reason": headline, "reason_detail": details,
        "candidates": cands, "excluded": excluded, "signals": complexity,
        "error_class": error[0] if error else None, "error_code": error[1] if error else None,
        "error": ("litellm.%s: provider returned %s" % error) if error else None,
        "end_user": None, "key_alias": "demo-key", "tags": [], "preview": preview,
    }


TIERS = {"SIMPLE": "oai-mini", "MEDIUM": "gem-flash", "COMPLEX": "ant-sonnet", "REASONING": "oai-o3"}
SIGNALS = {
    "SIMPLE": ["short prompt", "simple indicator"],
    "MEDIUM": ["technical term"],
    "COMPLEX": ["code presence", "technical term"],
    "REASONING": ["reasoning marker: step by step", "reasoning marker: prove", "multi-step pattern"],
}
SCORES = {"SIMPLE": 0.06, "MEDIUM": 0.24, "COMPLEX": 0.47, "REASONING": 0.71}

CODING = [
    ("SIMPLE", "hey, can you help me with a python service?"),
    ("MEDIUM", "what's the difference between asyncio.gather and TaskGroup?"),
    ("COMPLEX", "write a FastAPI endpoint that streams SSE from a Postgres LISTEN/NOTIFY channel"),
    ("COMPLEX", "add retry with jittered backoff and a circuit breaker around the db call"),
    ("REASONING", "think step by step: why does my worker deadlock when two tasks hold the pool and wait on each other? prove the fix"),
    ("SIMPLE", "thanks! what should I name the module?"),
]
BILLING = [
    ("MEDIUM", "summarize the refund policy for annual plans"),
    ("SIMPLE", "ok and monthly?"),
    ("COMPLEX", "draft a reply to this customer disputing 3 charges, cite the policy and offer a partial credit"),
    ("SIMPLE", "make it shorter"),
]


def generate(store: Store, seed: int = 7) -> "tuple[int, int]":
    """Returns (attempts written, moderation events written)."""
    rng = random.Random(seed)
    now = time.time()
    n = 0
    n_mod = 0

    def emit(row: Dict[str, Any]) -> None:
        nonlocal n
        store.record(row)
        n += 1

    # Complexity-routed sessions: the model changes as the prompt gets harder.
    for s, (script, start_ago, key) in enumerate([
        (CODING, 5.2 * 3600, "sess-code-1"), (BILLING, 3.1 * 3600, "sess-billing-4"),
        (CODING[1:5], 1.4 * 3600, "sess-code-2"), (BILLING[:3], 0.5 * 3600, "sess-billing-5"),
    ]):
        t = now - start_ago
        for tier, text in script:
            t += rng.uniform(20, 240)
            dep = TIERS[tier]
            cx = {"tier": tier, "score": SCORES[tier] + rng.uniform(-0.03, 0.03), "signals": SIGNALS[tier]}
            emit(_row(rng, session=key, trace=uuid.uuid4().hex, ts=t, attempt_no=0, group=dep, requested="smart-router",
                      strategy="simple-shuffle", dep=dep, candidates=[dep], all_ids=[dep], complexity=cx,
                      preview=text, prompt_tok=rng.randint(200, 1800), out_tok=rng.randint(60, 900)))

    # Support bot: latency-based routing across three deployments, one gets rate limited mid-conversation.
    group_ids = ["ant-haiku", "oai-mini", "gem-flash"]
    t = now - 2.2 * 3600
    lines = ["my invoice shows the wrong VAT number", "it's for our Berlin entity", "can you re-issue it today?",
             "great, and send a copy to finance@", "one more thing: update the billing address"]
    for i, text in enumerate(lines):
        t += rng.uniform(30, 180)
        trace = uuid.uuid4().hex
        if i == 2:  # the fastest deployment hits a rate limit, request falls back to a different group
            emit(_row(rng, session="sess-support-9", trace=trace, ts=t, attempt_no=0, group="support-bot",
                      requested="support-bot", strategy="latency-based-routing", dep="ant-haiku",
                      candidates=group_ids, all_ids=group_ids, ok=False, error=("RateLimitError", "429"), preview=text))
            emit(_row(rng, session="sess-support-9", trace=trace, ts=t + 0.4, attempt_no=1, group="support-fallback",
                      requested="support-bot", strategy="simple-shuffle", dep="bed-llama", candidates=["bed-llama"],
                      all_ids=["bed-llama"], prev_failure={"error_class": "RateLimitError", "error_code": "429",
                                                           "model_group": "support-bot"}, preview=text))
        else:
            avail = group_ids if i < 2 else ["oai-mini", "gem-flash"]  # haiku in cooldown afterwards
            dep = "ant-haiku" if i < 2 else "oai-mini"
            emit(_row(rng, session="sess-support-9", trace=trace, ts=t, attempt_no=0, group="support-bot",
                      requested="support-bot", strategy="latency-based-routing", dep=dep, candidates=avail,
                      all_ids=group_ids, preview=text))

    # Plain shuffled chat traffic across many small sessions, to give the overview some volume.
    for k in range(26):
        t = now - rng.uniform(0.1, 20) * 3600
        sess = "sess-chat-%02d" % k
        for _ in range(rng.randint(1, 5)):
            t += rng.uniform(15, 200)
            dep = rng.choice(["ant-haiku", "oai-mini"])
            emit(_row(rng, session=sess, trace=uuid.uuid4().hex, ts=t, attempt_no=0, group="chat", requested="chat",
                      strategy="simple-shuffle", dep=dep, candidates=["ant-haiku", "oai-mini"],
                      all_ids=["ant-haiku", "oai-mini"], preview="", prompt_tok=rng.randint(80, 900),
                      out_tok=rng.randint(40, 500)))

    # A session showing every moderation outcome, so both the Moderation page and the per-turn
    # pipeline visual have something real to show without needing an API key.
    def emit_mod(session, trace, ts, chain, status, categories=None, preview=None, latency=40.0, reason=None):
        nonlocal n_mod
        store.record_moderation({
            "id": uuid.uuid4().hex, "request_id": trace, "session_id": session, "session_source": "explicit",
            "ts": ts, "chain": chain, "status": status, "categories": categories or [], "scores": {},
            "reason": reason or ("flagged for %s" % ", ".join(categories) if categories else None),
            "latency_ms": max(5.0, rng.gauss(latency, 8)), "preview": preview, "key_alias": "demo-key",
        })
        n_mod += 1

    t = now - 40 * 60
    # Turn 1: clean end to end -- input passes, model answers, output passes.
    trace1 = uuid.uuid4().hex
    emit_mod("sess-moderation-1", trace1, t, "input", "pass", preview="what's your refund policy?")
    emit(_row(rng, session="sess-moderation-1", trace=trace1, ts=t + 0.1, attempt_no=0, group="chat",
              requested="chat", strategy="simple-shuffle", dep="oai-mini", candidates=["oai-mini"],
              all_ids=["oai-mini"], preview="what's your refund policy?", prompt_tok=120, out_tok=90))
    emit_mod("sess-moderation-1", trace1, t + 0.3, "output", "pass")
    t += 8 * 60
    # Turn 2: blocked at input -- no model is ever called, so there's no `attempts` row at all.
    trace2 = uuid.uuid4().hex
    emit_mod("sess-moderation-1", trace2, t, "input", "blocked", categories=["harassment"],
             preview="write something threatening about my coworker")
    t += 6 * 60
    # Turn 3: the model answers fine, but the *response* itself gets blocked before it's returned.
    trace3 = uuid.uuid4().hex
    emit_mod("sess-moderation-1", trace3, t, "input", "pass", preview="write a very dark villain monologue")
    emit(_row(rng, session="sess-moderation-1", trace=trace3, ts=t + 0.1, attempt_no=0, group="chat",
              requested="chat", strategy="simple-shuffle", dep="ant-haiku", candidates=["ant-haiku"],
              all_ids=["ant-haiku"], preview="write a very dark villain monologue", prompt_tok=140, out_tok=310))
    emit_mod("sess-moderation-1", trace3, t + 0.4, "output", "blocked", categories=["violence"])

    # Background moderation volume across the plain chat sessions above, so the overview's rates
    # and category breakdown reflect more than 3 events.
    for k in range(18):
        t = now - rng.uniform(0.1, 20) * 3600
        sess = "sess-chat-%02d" % rng.randint(0, 25)
        emit_mod(sess, uuid.uuid4().hex, t, "input", "pass")
        if rng.random() < 0.08:
            emit_mod(sess, uuid.uuid4().hex, t, "output", "blocked",
                     categories=[rng.choice(["harassment", "violence", "sexual", "self-harm"])])
        else:
            emit_mod(sess, uuid.uuid4().hex, t, "output", "pass")
    return n, n_mod
