from __future__ import annotations

from urirun_flow.env_selection import resolve_env_enums


ROUTE = "kvm://host/screen/query/capture"
ROUTES = [{
    "uri": ROUTE,
    "meta": {"contract": {"domains": {"monitor": {
        "type": "enum",
        "domain": "env:monitors.id",
        "optional": True,
        "emptyValues": [0, ""],
        "preference": "screen.capture.default",
        "skipWhen": {"scope": ["all", "all-monitors", "desktop"]},
    }}}},
}]


def _flow(payload=None):
    return {"steps": [{"id": "cap", "uri": ROUTE, "payload": payload or {}}]}


def _inventory(options, fingerprint="env-1"):
    return {"host": {"node": "host", "fingerprint": fingerprint,
                     "domains": {"env:monitors.id": options}}}


def test_explicit_env_enum_value_wins():
    res = resolve_env_enums(_flow({"monitor": 2}), ROUTES, _inventory([
        {"value": 1, "label": "HDMI-1"},
        {"value": 2, "label": "DP-2"},
    ]))

    assert res["ok"] is True
    assert res["flow"]["steps"][0]["payload"]["monitor"] == 2
    assert res["decisions"][0]["source"] == "explicit"


def test_single_option_is_selected_without_prompting():
    res = resolve_env_enums(_flow(), ROUTES, _inventory([
        {"value": 1, "label": "primary"},
    ]))

    assert res["ok"] is True
    assert res["flow"]["steps"][0]["payload"]["monitor"] == 1
    assert res["decisions"][0]["source"] == "single"


def test_remembered_value_is_fingerprint_keyed():
    class Memory:
        def recall_preference(self, node, name, fingerprint):
            assert (node, name, fingerprint) == ("host", "screen.capture.default", "env-dock")
            return {"value": {"monitor": 2}}

    res = resolve_env_enums(_flow(), ROUTES, _inventory([
        {"value": 1, "label": "HDMI-1"},
        {"value": 2, "label": "DP-2"},
    ], fingerprint="env-dock"), memory=Memory())

    assert res["ok"] is True
    assert res["flow"]["steps"][0]["payload"]["monitor"] == 2
    assert res["decisions"][0]["source"] == "remembered"


def test_multiple_options_without_memory_emits_needs_selection():
    res = resolve_env_enums(_flow(), ROUTES, _inventory([
        {"value": 1, "label": "HDMI-1"},
        {"value": 2, "label": "DP-2"},
    ]))

    assert res["ok"] is False
    assert res["kind"] == "needs-selection"
    assert res["needsSelection"]["parameter"] == "monitor"
    assert len(res["needsSelection"]["options"]) == 2


def test_skip_when_scope_all_does_not_ask_for_monitor():
    res = resolve_env_enums(_flow({"scope": "all", "monitor": -1}), ROUTES, _inventory([
        {"value": 1, "label": "HDMI-1"},
        {"value": 2, "label": "DP-2"},
    ]))

    assert res["ok"] is True
    assert res["flow"]["steps"][0]["payload"] == {"scope": "all", "monitor": -1}
