from __future__ import annotations

from urirun_flow.recovery import recovery_actions


def _ids(error: dict) -> list[str]:
    return [action["id"] for action in recovery_actions(error)]


def test_llm_provider_quota_actions_override_generic_invalid_argument():
    message = (
        "NL flow generated no URI steps; planner reason: litellm.APIError: "
        'OpenrouterException - {"error":{"message":"Key limit exceeded","code":403}}'
    )

    assert _ids({"category": "INVALID_ARGUMENT", "message": message}) == [
        "switch-llm-model-or-key",
        "retry-no-llm",
        "use-retrieved-known-good",
    ]


def test_plain_invalid_argument_still_repairs_payload():
    assert _ids({"category": "INVALID_ARGUMENT", "message": "bad payload"}) == ["repair-payload"]
