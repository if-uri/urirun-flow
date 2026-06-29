from __future__ import annotations

from urirun_flow.diagnostics import diagnose


def _planner_step() -> dict:
    return {"id": "plan", "uri": "flow://host/planner/command/make"}


def test_openrouter_key_limit_is_llm_quota_not_node_or_auth_failure():
    msg = (
        "NL flow generated no URI steps. Discovered 225 safe route(s) on node(s) ['host']; "
        "selected ['host']; planner reason: litellm.APIError: OpenrouterException - "
        '{"error":{"message":"Key limit exceeded (total limit)","code":403}}'
    )

    plan = diagnose({"message": msg, "category": "INVALID_ARGUMENT"}, step=_planner_step())

    assert plan is not None
    assert plan["rule"] == "llm-provider-quota"
    ids = [action["id"] for action in plan["remediation"]]
    assert "switch-llm-model-or-key" in ids
    assert "retry-no-llm" in ids


def test_missing_api_key_still_uses_auth_required_rule():
    plan = diagnose({"message": "api key missing", "category": "UNAUTHENTICATED"},
                    step={"id": "ask", "uri": "llm://host/chat/command/ask"})

    assert plan is not None
    assert plan["rule"] == "auth-required"
