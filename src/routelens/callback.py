"""LiteLLM callback that records every routing decision.

Install with `routelens install --config config.yaml --patch`, which adds:

    litellm_settings:
      callbacks: routelens_callback.instance

The callback never modifies requests or responses, and every hook swallows its
own errors so it can't break routing.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from litellm.integrations.custom_logger import CustomLogger

from .explain import EXCLUDED_GENERIC, explain
from .store import Store

log = logging.getLogger("routelens")

_complexity_var: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar(
    "routelens_complexity", default=None)
_patch_lock = threading.Lock()


def _install_complexity_patch() -> None:
    """LiteLLM's complexity router returns (tier, score, signals) but never logs them.
    Wrap classify() so we can attach that reasoning to the routing decision."""
    try:
        from litellm.router_strategy.complexity_router.complexity_router import ComplexityRouter
    except Exception:
        return
    with _patch_lock:
        if getattr(ComplexityRouter.classify, "_routelens", False):
            return
        original = ComplexityRouter.classify

        def classify(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            result = original(self, *args, **kwargs)
            try:
                tier, score, signals = result
                _complexity_var.set({
                    "tier": getattr(tier, "value", str(tier)),
                    "score": round(float(score), 3),
                    "signals": list(signals or []),
                })
            except Exception:
                pass
            return result

        classify._routelens = True  # type: ignore[attr-defined]
        ComplexityRouter.classify = classify  # type: ignore[method-assign]


def _provider_of(model: Optional[str], params: Optional[Dict[str, Any]] = None) -> str:
    params = params or {}
    if params.get("custom_llm_provider"):
        return str(params["custom_llm_provider"])
    if model and "/" in model:
        return model.split("/", 1)[0]
    try:
        import litellm
        return str(litellm.get_llm_provider(model)[1])  # type: ignore[index]
    except Exception:
        return "unknown"


def _short_model(model: Optional[str], provider: Optional[str]) -> str:
    model = model or "unknown"
    if provider and model.startswith(provider + "/"):
        return model[len(provider) + 1:]
    return model


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _first(messages: Optional[List[Dict[str, Any]]], role: str) -> str:
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == role:
            return _text_of(m.get("content"))
    return ""


def _last(messages: Optional[List[Dict[str, Any]]], role: str) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == role:
            return _text_of(m.get("content"))
    return ""


class RouteLens(CustomLogger):
    def __init__(
        self,
        db_path: Optional[str] = None,
        capture_content: Optional[str] = None,
        retention_days: Optional[float] = None,
        infer_sessions: Optional[bool] = None,
        mount: bool = True,
        router: Any = None,
    ) -> None:
        super().__init__()
        env = os.environ.get
        self.db_path = db_path or env("ROUTELENS_DB", "routelens.db")
        self.capture_content = (capture_content or env("ROUTELENS_CAPTURE_CONTENT", "none")).lower()
        days = retention_days if retention_days is not None else float(env("ROUTELENS_RETENTION_DAYS", "14"))
        self.infer_sessions = (
            infer_sessions if infer_sessions is not None
            else env("ROUTELENS_INFER_SESSIONS", "1").lower() not in ("0", "false", "no"))
        self.store = Store(self.db_path, retention_days=days)
        self._router = router
        self._pending: List[Dict[str, Any]] = []  # decisions awaiting their success/failure event, oldest first
        self._traces: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._sdk_reqs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        _install_complexity_patch()
        if mount:
            self._mount()

    def attach(self, router: Any) -> "RouteLens":
        """SDK users: give RouteLens your litellm.Router so it can see all deployments."""
        self._router = router
        return self

    # ------------------------------------------------------------------ mount
    def _mount(self) -> None:
        try:
            from litellm.proxy.proxy_server import app  # only importable inside the proxy process
            from .api import build_router
        except Exception:
            return
        try:
            app.include_router(build_router(self.store))
            log.warning("RouteLens dashboard mounted at /routelens (db: %s)", self.db_path)
        except Exception as e:  # pragma: no cover
            log.error("RouteLens could not mount its dashboard: %r", e)

    def _get_router(self) -> Any:
        if self._router is not None:
            return self._router
        try:
            from litellm.proxy import proxy_server
            return getattr(proxy_server, "llm_router", None)
        except Exception:
            return None

    # ------------------------------------------------------------ bookkeeping
    def _trace(self, trace_id: str) -> Dict[str, Any]:
        st = self._traces.get(trace_id)
        if st is None:
            st = {"attempts": 0, "last_failure": None, "failed_ids": []}
            self._traces[trace_id] = st
            while len(self._traces) > 5000:
                self._traces.popitem(last=False)
        else:
            self._traces.move_to_end(trace_id)
        return st

    def _unstamped_request_id(self, trace_id: str) -> str:
        """No proxy hook stamped an id (SDK use). A trace id can be shared by many requests, so attempts
        belong to the same request until one succeeds, or the trace goes quiet for a minute."""
        now = time.time()
        st = self._sdk_reqs.get(trace_id)
        if st is None or st["done"] or now - st["last"] > 60:
            st = {"req_id": uuid.uuid4().hex, "done": False, "last": now}
            self._sdk_reqs[trace_id] = st
            while len(self._sdk_reqs) > 5000:
                self._sdk_reqs.popitem(last=False)
        st["last"] = now
        self._sdk_reqs.move_to_end(trace_id)
        return st["req_id"]

    def _push(self, ctx: Dict[str, Any]) -> None:
        now = time.time()
        self._pending = [c for c in self._pending if now - c["ts"] < 600][-4999:]  # drop abandoned requests
        self._pending.append(ctx)

    def _pop(self, kwargs: Dict[str, Any], sl: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Pair a finished call with the routing decision made just before it.

        No single id is reliable: the proxy sets litellm_call_id but the SDK Router doesn't, and LiteLLM
        rewrites the logged trace_id to the session id when one is set. So score several signals and take
        the oldest decision with the best score (calls in one request finish in order)."""
        call_id = kwargs.get("litellm_call_id")
        logging_id = id(kwargs.get("litellm_logging_obj")) if kwargs.get("litellm_logging_obj") is not None else None
        model_id = sl.get("model_id") or (sl.get("hidden_params") or {}).get("model_id")
        best, best_score = None, 0
        for c in self._pending:
            score = 0
            if call_id and c["call_id"] == call_id:
                score = 4
            elif logging_id is not None and c["logging_id"] == logging_id:
                score = 3
            elif sl.get("trace_id") and c["filter_trace"] == sl.get("trace_id"):
                score = 2
            elif c["group"] == sl.get("model_group") and model_id in c["cand_ids"]:
                score = 1
            if score > best_score:
                best, best_score = c, score
        if best is not None:
            self._pending.remove(best)
        return best

    # ------------------------------------------------------------------ hooks
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # type: ignore[override]
        """Runs once per HTTP request. LiteLLM's trace id is shared across requests whenever a client
        sends x-litellm-session-id, so we stamp our own id to tell one turn from the next."""
        try:
            key = "litellm_metadata" if isinstance(data.get("litellm_metadata"), dict) else "metadata"
            if not isinstance(data.get(key), dict):
                data[key] = {}
            data[key].setdefault("routelens_request_id", uuid.uuid4().hex)
        except Exception as e:
            log.debug("routelens pre_call hook error: %r", e)
        return None

    async def async_post_call_success_deployment_hook(self, request_data, response, call_type):  # type: ignore[override]
        """Runs inline just before the response is returned (unlike the success log, which is a background
        task), so the next request on a shared trace id can't be mistaken for a retry. Returns None: we never
        touch the response."""
        try:
            st = self._sdk_reqs.get(str((request_data or {}).get("litellm_trace_id")))
            if st is not None:
                st["done"] = True
        except Exception:
            pass
        return None

    async def async_filter_deployments(  # type: ignore[override]
        self, model, healthy_deployments, messages, request_kwargs=None, parent_otel_span=None
    ):
        """Runs just before the strategy picks a winner: this is the candidate list."""
        try:
            self._on_candidates(model, healthy_deployments, messages, request_kwargs or {})
        except Exception as e:
            log.debug("routelens filter hook error: %r", e)
        return healthy_deployments

    def _on_candidates(self, group, healthy, messages, rk) -> None:
        call_id = rk.get("litellm_call_id")
        trace_id = str(rk.get("litellm_trace_id") or call_id or uuid.uuid4().hex)
        md = rk.get("metadata") or {}
        lmd = rk.get("litellm_metadata") or {}
        stamped = md.get("routelens_request_id") or lmd.get("routelens_request_id")
        req_id = str(stamped) if stamped else self._unstamped_request_id(trace_id)
        router = self._get_router()

        def describe(d: Dict[str, Any]) -> Dict[str, Any]:
            lp = d.get("litellm_params") or {}
            provider = _provider_of(lp.get("model"), lp)
            return {
                "id": (d.get("model_info") or {}).get("id"),
                "model": _short_model(lp.get("model"), provider),
                "provider": provider,
                "weight": lp.get("weight"),
            }

        candidates = [describe(d) for d in healthy or []]
        cand_ids = {c["id"] for c in candidates}
        trace = self._trace(req_id)
        excluded: List[Dict[str, Any]] = []
        try:
            if router is not None:
                for d in router.get_model_list(model_name=group) or []:
                    info = describe(d)
                    if info["id"] not in cand_ids:
                        info["why"] = ("Already failed in this request" if info["id"] in trace["failed_ids"]
                                       else EXCLUDED_GENERIC)
                        excluded.append(info)
        except Exception:
            pass

        session_id, source = self._session(rk, md, messages)
        body = ((rk.get("proxy_server_request") or {}).get("body") or {})
        ctx = {
            "uid": uuid.uuid4().hex, "request_id": req_id, "trace_id": None if stamped else trace_id,
            "call_id": call_id, "filter_trace": trace_id, "cand_ids": [c["id"] for c in candidates],
            "logging_id": id(rk["litellm_logging_obj"]) if rk.get("litellm_logging_obj") is not None else None,
            "session_id": session_id, "session_source": source, "ts": time.time(),
            "attempt_no": trace["attempts"], "prev_failure": trace["last_failure"],
            "group": group, "requested_model": body.get("model") or group,
            "strategy": getattr(router, "routing_strategy", None) if router is not None else None,
            "candidates": candidates, "excluded": excluded,
            "complexity": _complexity_var.get(),
            "tags": md.get("tags") or [],
            "end_user": md.get("user_api_key_end_user_id") or rk.get("user"),
            "key_alias": md.get("user_api_key_alias") or md.get("user_api_key_team_alias"),
            "preview": None,
        }
        if self.capture_content == "preview":
            txt = _last(messages, "user").strip()
            ctx["preview"] = (txt[:200] + "…") if len(txt) > 200 else txt
        trace["attempts"] += 1
        self._push(ctx)

    def _session(self, rk: Dict[str, Any], md: Dict[str, Any], messages) -> "tuple[str, str]":
        headers = {str(k).lower(): v for k, v in (md.get("headers") or {}).items()}
        for v in (
            rk.get("litellm_session_id"), md.get("session_id"), md.get("conversation_id"),
            headers.get("x-litellm-session-id"), headers.get("x-session-id"), headers.get("x-conversation-id"),
        ):
            if v:
                return str(v), "explicit"
        if self.infer_sessions:
            first_user = _first(messages, "user").strip()
            if first_user:
                seed = "|".join([md.get("user_api_key_hash") or "", _first(messages, "system"), first_user])
                return "auto-" + hashlib.sha256(seed.encode()).hexdigest()[:10], "inferred"
        return "req-" + str(rk.get("litellm_trace_id") or rk.get("litellm_call_id") or uuid.uuid4().hex)[:10], "none"

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            self._on_complete(kwargs, start_time, end_time, ok=True)
        except Exception as e:
            log.debug("routelens success hook error: %r", e)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            self._on_complete(kwargs, start_time, end_time, ok=False)
        except Exception as e:
            log.debug("routelens failure hook error: %r", e)

    def _on_complete(self, kwargs: Dict[str, Any], start_time: Any, end_time: Any, ok: bool) -> None:
        sl = kwargs.get("standard_logging_object") or {}
        call_id = kwargs.get("litellm_call_id")
        ctx = self._pop(kwargs, sl)
        if ctx is None:
            ctx = self._direct_ctx(kwargs, sl, call_id)

        params = kwargs.get("litellm_params") or {}
        provider = sl.get("custom_llm_provider") or _provider_of(sl.get("model"), params)
        model = _short_model(sl.get("model"), provider)
        chosen_id = sl.get("model_id") or (sl.get("hidden_params") or {}).get("model_id")
        if ok and ctx.get("trace_id") in self._sdk_reqs:
            self._sdk_reqs[ctx["trace_id"]]["done"] = True

        try:
            latency_ms = (end_time - start_time).total_seconds() * 1000.0
        except Exception:
            latency_ms = (sl.get("response_time") or 0) * 1000.0

        err = sl.get("error_information") or {}
        if not ok and chosen_id:
            trace = self._trace(ctx["request_id"])
            trace["failed_ids"].append(chosen_id)
            trace["last_failure"] = {
                "error_class": err.get("error_class"), "error_code": err.get("error_code"),
                "model_group": ctx["group"], "model": model,
            }

        kind, headline, details = explain(
            strategy=ctx["strategy"], group=ctx["group"], requested_model=ctx["requested_model"],
            candidates=ctx["candidates"], excluded=ctx["excluded"], chosen_id=chosen_id,
            prev_failure=ctx["prev_failure"], complexity=ctx["complexity"], tags=ctx["tags"],
        ) if ctx["strategy"] != "__direct__" else (
            "direct", "Called directly, not through a model group", [])

        self.store.record({
            "call_id": ctx["uid"], "request_id": ctx["request_id"], "session_id": ctx["session_id"],
            "session_source": ctx["session_source"], "ts": ctx["ts"], "attempt_no": ctx["attempt_no"],
            "requested_model": ctx["requested_model"], "model_group": ctx["group"],
            "deployment_id": chosen_id, "model": model, "provider": provider,
            "status": "success" if ok else "failure", "latency_ms": latency_ms,
            "cost": float(sl.get("response_cost") or 0.0),
            "prompt_tokens": int(sl.get("prompt_tokens") or 0),
            "completion_tokens": int(sl.get("completion_tokens") or 0),
            "cache_hit": 1 if sl.get("cache_hit") else 0,
            "strategy": None if ctx["strategy"] == "__direct__" else ctx["strategy"],
            "reason_kind": kind, "reason": headline, "reason_detail": details,
            "candidates": ctx["candidates"], "excluded": ctx["excluded"], "signals": ctx["complexity"],
            "error_class": err.get("error_class"), "error_code": err.get("error_code"),
            "error": (sl.get("error_str") or "")[:400] or None,
            "end_user": ctx["end_user"], "key_alias": ctx["key_alias"], "tags": ctx["tags"],
            "preview": ctx["preview"],
        })

    def _direct_ctx(self, kwargs: Dict[str, Any], sl: Dict[str, Any], call_id: Optional[str]) -> Dict[str, Any]:
        """A call that never went through the router's filter hook (e.g. litellm.completion())."""
        trace_id = sl.get("trace_id") or call_id or uuid.uuid4().hex
        md = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        trace_id = md.get("routelens_request_id") or trace_id
        session_id, source = self._session(
            {"litellm_session_id": (kwargs.get("litellm_params") or {}).get("litellm_session_id"),
             "litellm_trace_id": trace_id}, md, kwargs.get("messages"))
        provider = sl.get("custom_llm_provider") or "unknown"
        chosen = {"id": sl.get("model_id"), "model": _short_model(sl.get("model"), provider), "provider": provider}
        return {
            "uid": uuid.uuid4().hex, "request_id": str(trace_id), "session_id": session_id,
            "session_source": source, "ts": float(sl.get("startTime") or time.time()), "attempt_no": 0,
            "prev_failure": None, "group": sl.get("model_group"), "requested_model": kwargs.get("model"),
            "strategy": "__direct__", "candidates": [chosen], "excluded": [], "complexity": None, "tags": [],
            "end_user": sl.get("end_user"), "key_alias": None, "preview": None,
        }
