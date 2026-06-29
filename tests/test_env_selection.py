from __future__ import annotations

from urirun_flow.env_selection import recall_env_enum_replan_required, resolve_env_enums


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


def test_invalid_explicit_env_enum_value_is_rejected():
    res = resolve_env_enums(_flow({"monitor": 99}), ROUTES, _inventory([
        {"value": 1, "label": "HDMI-1"},
        {"value": 2, "label": "DP-2"},
    ]))

    assert res["ok"] is False
    assert res["kind"] == "env-domain-invalid"
    assert res["violation"]["allowed"] == [1, 2]


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


def test_result_reference_defers_env_enum_until_execution():
    res = resolve_env_enums(
        _flow({"monitor_from": "list_windows.result.value.selected.monitor"}),
        ROUTES,
        _inventory([
            {"value": 1, "label": "HDMI-1"},
            {"value": 2, "label": "DP-2"},
        ]),
    )

    assert res["ok"] is True
    assert res["decisions"][0]["source"] == "result-ref"


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
    assert res["decisions"][0]["source"] == "skip"


def test_recalled_scope_all_requires_replan_when_domain_has_multiple_options():
    res = recall_env_enum_replan_required(
        _flow({"scope": "all", "monitor": -1}),
        ROUTES,
        _inventory([
            {"value": 1, "label": "HDMI-1"},
            {"value": 2, "label": "DP-2"},
            {"value": 3, "label": "DP-1"},
        ]),
    )

    assert res["required"] is True
    assert res["reason"] == "skip-when"
    assert res["parameter"] == "monitor"


def test_recalled_concrete_valid_env_enum_can_shortcut():
    res = recall_env_enum_replan_required(
        _flow({"monitor": 3}),
        ROUTES,
        _inventory([
            {"value": 1, "label": "HDMI-1"},
            {"value": 2, "label": "DP-2"},
            {"value": 3, "label": "DP-1"},
        ]),
    )

    assert res["required"] is False


def test_one_env_enum_resolver_handles_multiple_device_domains():
    class Memory:
        def recall_preference(self, node, name, fingerprint):
            assert (node, fingerprint) == ("host", "env-interop")
            if name == "audio.default":
                return {"value": "usb"}
            return None

    routes = [
        {"uri": "kvm://host/camera/query/snap",
         "meta": {"contract": {"domains": {"camera": {
             "type": "enum", "domain": "env:cameras.id", "preference": "camera.default"}}}}},
        {"uri": "audio://host/sink/command/route",
         "meta": {"contract": {"domains": {"sink": {
             "type": "enum", "domain": "env:audio_sinks.id", "preference": "audio.default"}}}}},
        *ROUTES,
    ]
    inventories = {"host": {"node": "host", "fingerprint": "env-interop", "domains": {
        "env:cameras.id": [{"value": 0}],
        "env:audio_sinks.id": [{"value": "hdmi"}, {"value": "usb"}],
        "env:monitors.id": [{"value": 1}, {"value": 2}, {"value": 3}],
    }}}

    camera = resolve_env_enums(
        {"steps": [{"id": "camera", "uri": "kvm://host/camera/query/snap", "payload": {}}]},
        routes, inventories, memory=Memory())
    audio = resolve_env_enums(
        {"steps": [{"id": "sink", "uri": "audio://host/sink/command/route", "payload": {}}]},
        routes, inventories, memory=Memory())
    monitor = resolve_env_enums(_flow(), routes, inventories, memory=Memory())

    assert camera["ok"] is True
    assert camera["flow"]["steps"][0]["payload"]["camera"] == 0
    assert camera["decisions"][0]["source"] == "single"
    assert audio["ok"] is True
    assert audio["flow"]["steps"][0]["payload"]["sink"] == "usb"
    assert audio["decisions"][0]["source"] == "remembered"
    assert monitor["kind"] == "needs-selection"
    assert monitor["needsSelection"]["parameter"] == "monitor"


def test_new_env_enum_device_needs_declaration_not_resolver_code():
    before = resolve_env_enums.__code__.co_code
    route = {"uri": "print://host/doc/command/print",
             "meta": {"contract": {"domains": {"printer": {
                 "type": "enum", "domain": "env:printers.id"}}}}}
    inventory = {"host": {"node": "host", "fingerprint": "env-printers", "domains": {
        "env:printers.id": [{"value": "hp"}, {"value": "epson"}],
    }}}

    res = resolve_env_enums(
        {"steps": [{"id": "print", "uri": "print://host/doc/command/print", "payload": {}}]},
        [route],
        inventory,
    )

    assert res["kind"] == "needs-selection"
    assert [opt["value"] for opt in res["needsSelection"]["options"]] == ["hp", "epson"]
    assert resolve_env_enums.__code__.co_code == before
