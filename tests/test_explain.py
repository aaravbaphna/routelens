from routelens.explain import explain

C = [{"id": "a", "model": "gpt-4o-mini", "provider": "openai"}, {"id": "b", "model": "haiku", "provider": "anthropic"}]


def run(**kw):
    base = dict(strategy="simple-shuffle", group="chat", requested_model="chat", candidates=C, excluded=[],
                chosen_id="a", prev_failure=None, complexity=None)
    base.update(kw)
    return explain(**base)


def test_strategy_choice_among_many():
    kind, headline, _ = run(strategy="latency-based-routing")
    assert kind == "latency" and "2 eligible" in headline


def test_single_candidate_is_only_option():
    kind, headline, _ = run(candidates=C[:1])
    assert kind == "only_option" and "chat" in headline


def test_excluded_are_counted():
    kind, headline, details = run(candidates=C[:1], excluded=[{"id": "b"}])
    assert kind == "only_option" and "1 unavailable" in headline and "1 excluded before selection" in details


def test_fallback_names_both_groups_and_error():
    kind, headline, _ = run(group="backup", prev_failure={"error_class": "RateLimitError", "error_code": "429", "model_group": "flaky"})
    assert kind == "fallback" and "flaky" in headline and "backup" in headline and "RateLimitError (429)" in headline


def test_same_group_failure_is_a_retry():
    kind, headline, _ = run(prev_failure={"error_class": "Timeout", "error_code": None, "model_group": "chat"})
    assert kind == "retry" and "Timeout" in headline


def test_complexity_wins_over_strategy():
    kind, headline, details = run(complexity={"tier": "REASONING", "score": 0.71, "signals": ["reasoning marker"]})
    assert kind == "complexity" and "REASONING" in headline and "0.71" in headline
    assert any("reasoning marker" in d for d in details)


def test_unknown_strategy_falls_back_to_generic():
    kind, headline, _ = run(strategy="my-custom-thing")
    assert kind == "strategy" and "my-custom-thing" in headline
