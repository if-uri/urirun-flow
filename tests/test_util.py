from __future__ import annotations

import sys

from urirun_flow import _util


def test_default_max_tokens_is_bounded(monkeypatch):
    monkeypatch.delenv("URIRUN_LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    assert _util._default_max_tokens() == 4096


def test_default_max_tokens_can_be_overridden(monkeypatch):
    monkeypatch.setenv("URIRUN_LLM_MAX_TOKENS", "1234")
    assert _util._default_max_tokens() == 1234


def test_default_max_tokens_ignores_invalid_values(monkeypatch):
    monkeypatch.setenv("URIRUN_LLM_MAX_TOKENS", "not-a-number")
    assert _util._default_max_tokens() == 4096


def test_quiet_completion_retries_with_fewer_tokens_on_provider_limit(monkeypatch):
    calls = []

    class FakeLiteLLM:
        suppress_debug_info = False

        @staticmethod
        def completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("This request requires more credits, or fewer max_tokens")
            return {"ok": True}

    monkeypatch.setitem(sys.modules, "litellm", FakeLiteLLM)
    monkeypatch.delenv("URIRUN_LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)

    assert _util.quiet_completion(model="x", messages=[]) == {"ok": True}
    assert [call["max_tokens"] for call in calls] == [4096, 1024]
