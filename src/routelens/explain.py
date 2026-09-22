"""Turn what we observed about a routing decision into a human-readable "why".

Everything here is derived from facts the router exposes (strategy, eligible
deployments, what was filtered out, the previous failure in the same request,
the complexity classifier's output). We do NOT re-run the strategy's scoring,
so explanations describe the rule that applied, not a recomputed ranking.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

STRATEGY_RULES = {
    "simple-shuffle": ("shuffle", "Picked at random among {n} eligible deployments"),
    "latency-based-routing": ("latency", "Lowest recent latency among {n} eligible deployments"),
    "cost-based-routing": ("cost", "Lowest cost among {n} eligible deployments"),
    "usage-based-routing": ("usage", "Lowest token/request usage among {n} eligible deployments"),
    "usage-based-routing-v2": ("usage", "Lowest token/request usage among {n} eligible deployments"),
    "least-busy": ("least_busy", "Fewest in-flight requests among {n} eligible deployments"),
}

EXCLUDED_GENERIC = "Not eligible (cooldown, rate limit, context window or tag filter)"


def _weights_note(candidates: List[Dict[str, Any]]) -> Optional[str]:
    weights = [(c.get("model"), c.get("weight")) for c in candidates if c.get("weight") is not None]
    if not weights:
        return None
    return "Weights: " + ", ".join("%s=%s" % (m, w) for m, w in weights)


def explain(
    *,
    strategy: Optional[str],
    group: Optional[str],
    requested_model: Optional[str],
    candidates: List[Dict[str, Any]],
    excluded: List[Dict[str, Any]],
    chosen_id: Optional[str],
    prev_failure: Optional[Dict[str, Any]],
    complexity: Optional[Dict[str, Any]],
    tags: Optional[List[str]] = None,
) -> Tuple[str, str, List[str]]:
    """Returns (kind, headline, details)."""
    details: List[str] = []
    n = len(candidates)

    if excluded:
        details.append("%d excluded before selection" % len(excluded))

    # 1. A previous attempt in this request failed: this is a retry or fallback.
    if prev_failure:
        err = prev_failure.get("error_class") or "error"
        code = prev_failure.get("error_code")
        err_txt = "%s (%s)" % (err, code) if code else err
        prev_group = prev_failure.get("model_group")
        if prev_group and group and prev_group != group:
            headline = "Fallback: `%s` failed with %s, so the request moved to `%s`" % (prev_group, err_txt, group)
            return "fallback", headline, details
        headline = "Retry after %s on `%s`" % (err_txt, group or "the same group")
        if n > 1:
            details.append("Re-selected among %d eligible deployments" % n)
        return "retry", headline, details

    # 2. The complexity router picked the model group from the prompt.
    if complexity:
        tier = complexity.get("tier")
        score = complexity.get("score")
        headline = "Prompt classified as %s (score %s), routed to `%s`" % (
            tier, ("%.2f" % score) if isinstance(score, (int, float)) else "n/a", group)
        if complexity.get("signals"):
            details.append("Signals: " + ", ".join(str(s) for s in complexity["signals"][:6]))
        return "complexity", headline, details

    # 3. Nothing to choose between.
    if n <= 1:
        if excluded:
            headline = "Only eligible deployment in `%s` (%d unavailable)" % (group, len(excluded))
        else:
            headline = "Only deployment configured for `%s`" % group
        return "only_option", headline, details

    # 4. Strategy-driven choice among several candidates.
    if tags:
        details.append("Request tags: " + ", ".join(tags))
    rule = STRATEGY_RULES.get(strategy or "")
    if rule:
        kind, template = rule
        w = _weights_note(candidates) if kind == "shuffle" else None
        if w:
            details.append(w)
        return kind, template.format(n=n), details
    return "strategy", "Chosen by `%s` among %d eligible deployments" % (strategy or "router", n), details
