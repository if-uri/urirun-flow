"""Resolve runtime enum parameters from Twin inventory before dispatch.

Contracts declare that a payload parameter draws its values from an environment
domain, e.g. ``monitor -> env:monitors.id``. This module is the single gate that
turns that declaration plus live inventory plus Twin memory into either a
concrete payload or a typed ``needs-selection`` request.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable


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


def flow_route_domains(flow: dict, routes: list[dict]) -> dict[str, dict]:
    """Return route-declared env domains used by this flow, keyed by URI."""
    by_uri = {str(r.get("uri") or ""): r for r in routes or []}
    out: dict[str, dict] = {}
    for step in flow.get("steps") or []:
        uri = str(step.get("uri") or "")
        contract = (by_uri.get(uri, {}).get("meta") or {}).get("contract") or {}
        domains = contract.get("domains") or {}
        if domains:
            out[uri] = domains
    return out


def env_enum_nodes(flow: dict, routes: list[dict]) -> list[str]:
    """Return target nodes whose steps need contract-declared env enum inventory."""
    domains = flow_route_domains(flow, routes)
    if not domains:
        return []
    nodes = {
        _target(str(step.get("uri") or ""))
        for step in flow.get("steps") or []
        if str(step.get("uri") or "") in domains
    }
    return sorted(nodes)


def build_env_enum_inventories(
    flow: dict,
    routes: list[dict],
    *,
    inventory_builder: Callable[[str], dict] | None = None,
) -> dict[str, dict]:
    """Build live inventory only for nodes whose flow steps declare env enum domains."""
    if inventory_builder is None:
        return {}
    return {node: inventory_builder(node) for node in env_enum_nodes(flow, routes)}


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


def _has_result_reference(payload: dict, param: str) -> bool:
    return isinstance(payload.get(f"{param}_from"), str) and bool(str(payload.get(f"{param}_from")).strip())


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


def _value_key(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return text.lower()
    return value


def _option_value_keys(options: list[dict]) -> set[Any]:
    return {_value_key(opt.get("value")) for opt in options}


def _nl_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    ascii_text = ascii_text.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    return ascii_text.casefold()


def _prompt_requests_primary(prompt: str) -> bool:
    return bool(re.search(r"\b(primary|main|glown\w*)\b", _nl_key(prompt)))


def _primary_option_value(options: list[dict]) -> Any:
    for opt in options:
        if opt.get("primary"):
            return opt.get("value")
    return None


def _env_domain_invalid(uri: str, node: str, param: str, cfg: dict, value: Any,
                        options: list[dict]) -> dict:
    return {
        "ok": False,
        "kind": "env-domain-invalid",
        "violation": {
            "kind": "env-domain-invalid",
            "uri": uri,
            "node": node,
            "parameter": param,
            "domain": cfg.get("domain"),
            "value": value,
            "allowed": [opt.get("value") for opt in options],
        },
        "next": {"kind": "replan", "reason": "env-domain-invalid"},
    }


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


def recall_env_enum_replan_required(flow: dict, routes: list[dict],
                                    inventories: dict[str, dict] | list[dict] | None) -> dict:
    """Return why a recalled flow must be treated as a proposal, not a shortcut.

    A remembered flow is safe to replay only when contract-declared env-enum
    parameters are already concrete and valid for the current inventory. If a
    recalled step bypasses the enum via ``skipWhen`` (for example ``scope: all``),
    leaves it unresolved, or carries a value outside the current domain, the LLM
    gets that flow through retrieval and must propose a fresh candidate.
    """
    for step in flow.get("steps") or []:
        uri = str(step.get("uri") or "")
        payload = dict(step.get("payload") or {})
        node = _target(uri)
        inventory = _inventory_for(node, inventories)
        for param, cfg in _route_domains(uri, routes).items():
            if not isinstance(cfg, dict) or cfg.get("type") != "enum" or not cfg.get("domain"):
                continue
            options = _domain_options(inventory, str(cfg["domain"]))
            if len(options) <= 1:
                continue
            reason = ""
            if _skip_by_payload(payload, cfg):
                reason = "skip-when"
            elif _has_result_reference(payload, str(param)):
                continue
            elif not _has_explicit(payload, str(param), cfg):
                reason = "unresolved"
            elif _value_key(payload.get(param)) not in _option_value_keys(options):
                reason = "invalid"
            if reason:
                return {
                    "required": True,
                    "reason": reason,
                    "uri": uri,
                    "node": node,
                    "parameter": str(param),
                    "domain": cfg.get("domain"),
                    "value": payload.get(param),
                    "optionCount": len(options),
                    "allowed": [opt.get("value") for opt in options],
                }
    return {"required": False}


def resolve_env_enums(flow: dict, routes: list[dict], inventories: dict[str, dict] | list[dict],
                      memory: Any = None, prompt: str = "") -> dict:
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
            if _skip_by_payload(payload, cfg):
                decisions.append({"uri": uri, "parameter": param, "source": "skip"})
                continue
            if _has_result_reference(payload, str(param)):
                decisions.append({"uri": uri, "parameter": param, "source": "result-ref",
                                  "from": payload.get(f"{param}_from")})
                continue
            options = _domain_options(inventory, str(cfg["domain"]))
            if _has_explicit(payload, param, cfg):
                if options and _value_key(payload.get(param)) not in _option_value_keys(options):
                    return {**_env_domain_invalid(uri, node, param, cfg, payload.get(param), options), "flow": flow}
                decisions.append({"uri": uri, "parameter": param, "source": "explicit"})
                continue
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
            primary_value = _primary_option_value(options) if _prompt_requests_primary(prompt) else None
            if primary_value in _option_values(options):
                payload[param] = primary_value
                decisions.append({"uri": uri, "parameter": param, "source": "primary",
                                  "value": primary_value})
                continue
            if len(options) > 1:
                return {**_needs_selection(uri, node, param, cfg, options, inventory), "flow": flow}
            decisions.append({"uri": uri, "parameter": param, "source": "unresolved"})
        out_steps.append({**step, "payload": payload})
    return {"ok": True, "flow": {**flow, "steps": out_steps}, "decisions": decisions}


def resolve_flow_env_enums(
    flow: dict,
    routes: list[dict],
    *,
    memory: Any = None,
    inventories: dict[str, dict] | list[dict] | None = None,
    inventory_builder: Callable[[str], dict] | None = None,
    prompt: str = "",
) -> dict:
    """Resolve env enum slots for a whole flow and attach the inventory used."""
    if not flow_route_domains(flow, routes):
        return {"ok": True, "flow": flow, "inventories": {}}
    resolved_inventories = (
        inventories
        if inventories is not None
        else build_env_enum_inventories(flow, routes, inventory_builder=inventory_builder)
    )
    result = resolve_env_enums(flow, routes, resolved_inventories or {}, memory=memory, prompt=prompt)
    return {**result, "inventories": resolved_inventories or {}}
