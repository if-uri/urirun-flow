"""Resolve runtime enum parameters from Twin inventory before dispatch.

Contracts declare that a payload parameter draws its values from an environment
domain, e.g. ``monitor -> env:monitors.id``. This module is the single gate that
turns that declaration plus live inventory plus Twin memory into either a
concrete payload or a typed ``needs-selection`` request.
"""
from __future__ import annotations

from typing import Any


def _target(uri: str) -> str:
    if "://" not in uri:
        return "host"
    rest = uri.split("://", 1)[1]
    return rest.split("/", 1)[0] or "host"


def _route_domains(uri: str, routes: list[dict]) -> dict:
    for route in routes or []:
        if str(route.get("uri") or "") != uri:
            continue
        contract = (route.get("meta") or {}).get("contract") or {}
        return contract.get("domains") or {}
    return {}


def _inventory_for(node: str, inventories: dict[str, dict] | list[dict] | None) -> dict:
    if isinstance(inventories, dict):
        inv = inventories.get(node) or inventories.get("host") or {}
        return inv if isinstance(inv, dict) else {}
    for inv in inventories or []:
        if isinstance(inv, dict) and str(inv.get("node") or "host") == node:
            return inv
    return {}


def _domain_options(inventory: dict, domain: str) -> list[dict]:
    raw = (inventory.get("domains") or {}).get(domain) or []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("value", item.get("id"))
            out.append({**item, "value": value, "label": str(item.get("label") or value)})
        else:
            out.append({"value": item, "label": str(item)})
    return [opt for opt in out if opt.get("value") is not None]


def _skip_by_payload(payload: dict, cfg: dict) -> bool:
    for key, allowed in (cfg.get("skipWhen") or {}).items():
        val = str(payload.get(key) or "").strip().lower()
        if val and val in {str(x).strip().lower() for x in (allowed or [])}:
            return True
    return False


def _has_explicit(payload: dict, param: str, cfg: dict) -> bool:
    if param not in payload:
        return False
    empty = set(cfg.get("emptyValues") or [None, ""])
    return payload.get(param) not in empty


def _preference_value(memory: Any, node: str, cfg: dict, param: str, fingerprint: str) -> Any:
    if memory is None or not hasattr(memory, "recall_preference"):
        return None
    pref_name = cfg.get("preference")
    if not pref_name:
        return None
    pref = memory.recall_preference(node, str(pref_name), fingerprint)
    value = (pref or {}).get("value") if isinstance(pref, dict) else None
    if isinstance(value, dict):
        return value.get(param)
    return value


def _option_values(options: list[dict]) -> set:
    return {opt.get("value") for opt in options}


def _needs_selection(uri: str, node: str, param: str, cfg: dict,
                     options: list[dict], inventory: dict) -> dict:
    return {
        "ok": False,
        "kind": "needs-selection",
        "needsSelection": {
            "uri": uri,
            "node": node,
            "parameter": param,
            "domain": cfg.get("domain"),
            "options": options,
            "default": None,
            "fingerprint": inventory.get("fingerprint"),
            "preference": cfg.get("preference"),
        },
        "next": {"kind": "needs-selection"},
    }


def resolve_env_enums(flow: dict, routes: list[dict], inventories: dict[str, dict] | list[dict],
                      memory: Any = None) -> dict:
    """Return ``{ok, flow, decisions}`` or ``needs-selection`` for unresolved env enums."""
    decisions: list[dict] = []
    out_steps: list[dict] = []
    for step in flow.get("steps") or []:
        uri = str(step.get("uri") or "")
        payload = dict(step.get("payload") or {})
        node = _target(uri)
        domains = _route_domains(uri, routes)
        inventory = _inventory_for(node, inventories)
        for param, cfg in domains.items():
            if not isinstance(cfg, dict) or cfg.get("type") != "enum" or not cfg.get("domain"):
                continue
            if _skip_by_payload(payload, cfg) or _has_explicit(payload, param, cfg):
                decisions.append({"uri": uri, "parameter": param, "source": "explicit"})
                continue
            options = _domain_options(inventory, str(cfg["domain"]))
            if len(options) == 1:
                payload[param] = options[0]["value"]
                decisions.append({"uri": uri, "parameter": param, "source": "single",
                                  "value": options[0]["value"]})
                continue
            pref_value = _preference_value(memory, node, cfg, param, str(inventory.get("fingerprint") or ""))
            if pref_value in _option_values(options):
                payload[param] = pref_value
                decisions.append({"uri": uri, "parameter": param, "source": "remembered",
                                  "value": pref_value})
                continue
            if len(options) > 1:
                return {**_needs_selection(uri, node, param, cfg, options, inventory), "flow": flow}
            decisions.append({"uri": uri, "parameter": param, "source": "unresolved"})
        out_steps.append({**step, "payload": payload})
    return {"ok": True, "flow": {**flow, "steps": out_steps}, "decisions": decisions}
