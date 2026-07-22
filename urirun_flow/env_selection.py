"""Resolve runtime enum parameters from Twin inventory before dispatch.

Contracts declare that a payload parameter draws its values from an environment
domain, e.g. ``monitor -> env:monitors.id``. This module is the single gate that
turns that declaration plus live inventory plus Twin memory into either a
concrete payload or a typed ``needs-selection`` request.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
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


def _drop_skip_fields(payload: dict, cfg: dict) -> None:
    """Remove the payload fields whose values trigger this domain's skipWhen bypass.

    Used when the prompt names one concrete option: the bypass value (e.g. capture
    scope:'all') was planner boilerplate, not user intent — dropping it lets the
    explicitly grounded option take effect."""
    for key, allowed in (cfg.get("skipWhen") or {}).items():
        val = str(payload.get(key) or "").strip().lower()
        if val and val in {str(x).strip().lower() for x in (allowed or [])}:
            payload.pop(key, None)


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


def _option_label_keys(opt: dict) -> set:
    keys = set()
    for field in ("label", "connector", "displayName", "name"):
        value = opt.get(field)
        if isinstance(value, str) and value.strip():
            keys.add(_nl_key(value.strip()))
    return keys


def _option_by_label(options: list[dict], value: Any) -> Any:
    """Coerce a label-like explicit value ('DP-2', 'AOC 32"') to its option value.

    LLM planners routinely name the human label instead of the contract's value
    (a monitor index); grounding the label here keeps the plan instead of
    discarding it as env-domain-invalid."""
    key = _nl_key(str(value if value is not None else "").strip())
    if not key:
        return None
    for opt in options:
        if key in _option_label_keys(opt):
            return opt.get("value")
    return None


def _option_from_prompt(options: list[dict], prompt: str) -> Any:
    """Pick the option whose label appears in the prompt, when exactly one does.

    'zrob zrzut ekranu monitora DP-2' resolves monitor=2 without a needs-selection
    round-trip; ambiguous prompts (two labels mentioned) still fall through."""
    text = _nl_key(prompt)
    if not text:
        return None
    hits = [opt.get("value") for opt in options
            if any(k and k in text for k in _option_label_keys(opt))]
    return hits[0] if len(hits) == 1 else None


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


@dataclass
class _EnumSlot:
    """One enum parameter of one step, with everything a rung needs to decide."""

    uri: str
    node: str
    param: str
    cfg: dict
    payload: dict
    options: list[dict]
    inventory: dict
    memory: Any
    prompt: str

    def decide(self, source: str, **extra: Any) -> dict:
        """A resolution for this parameter, recorded in ``decisions``."""
        return {"decision": {"uri": self.uri, "parameter": self.param, "source": source, **extra}}


def _rung_skip(slot: _EnumSlot) -> dict | None:
    """Payload says this parameter does not apply (e.g. scope:'all')."""
    if not _skip_by_payload(slot.payload, slot.cfg):
        return None
    # A prompt that names exactly ONE option label overrides the bypass:
    # "zrzut ekranu monitora HDMI-1" with a planner-emitted scope:'all' means
    # the user wants THAT monitor — the skip value was boilerplate.
    prompt_value = _option_from_prompt(slot.options, slot.prompt)
    if prompt_value is not None and prompt_value in _option_values(slot.options):
        _drop_skip_fields(slot.payload, slot.cfg)
        slot.payload[slot.param] = prompt_value
        return slot.decide("prompt-label-over-skip", value=prompt_value)
    return slot.decide("skip")


def _rung_result_ref(slot: _EnumSlot) -> dict | None:
    """The value arrives at runtime from an earlier step's result."""
    if not _has_result_reference(slot.payload, str(slot.param)):
        return None
    return slot.decide("result-ref", **{"from": slot.payload.get(f"{slot.param}_from")})


def _rung_explicit(slot: _EnumSlot) -> dict | None:
    """The payload already carries a value — validate, coerce, or let a label win."""
    if not _has_explicit(slot.payload, slot.param, slot.cfg):
        return None
    given = slot.payload.get(slot.param)
    if slot.options and _value_key(given) not in _option_value_keys(slot.options):
        coerced = _option_by_label(slot.options, given)
        if coerced is None:
            return {"terminal": _env_domain_invalid(
                slot.uri, slot.node, slot.param, slot.cfg, given, slot.options)}
        slot.payload[slot.param] = coerced
        return slot.decide("label", value=coerced)
    # Valid-but-conflicting guard: the user named ONE option by label and the
    # planner's numeric value points at a different one ("monitora DP-1" but
    # monitor:1 = HDMI-1) — the user's label wins over the model's guess.
    prompt_value = _option_from_prompt(slot.options, slot.prompt)
    if (prompt_value is not None and prompt_value in _option_values(slot.options)
            and _value_key(prompt_value) != _value_key(given)):
        slot.payload[slot.param] = prompt_value
        return slot.decide("prompt-label-over-explicit", value=prompt_value)
    return slot.decide("explicit")


def _rung_single(slot: _EnumSlot) -> dict | None:
    """Exactly one option exists — no choice to make."""
    if len(slot.options) != 1:
        return None
    slot.payload[slot.param] = slot.options[0]["value"]
    return slot.decide("single", value=slot.options[0]["value"])


def _rung_remembered(slot: _EnumSlot) -> dict | None:
    """Twin memory holds a preference for this node/domain fingerprint."""
    pref_value = _preference_value(slot.memory, slot.node, slot.cfg, slot.param,
                                   str(slot.inventory.get("fingerprint") or ""))
    if pref_value not in _option_values(slot.options):
        return None
    slot.payload[slot.param] = pref_value
    return slot.decide("remembered", value=pref_value)


def _rung_primary(slot: _EnumSlot) -> dict | None:
    """The prompt asks for the primary/main one (e.g. "główny monitor")."""
    if not _prompt_requests_primary(slot.prompt):
        return None
    primary_value = _primary_option_value(slot.options)
    if primary_value is None or primary_value not in _option_values(slot.options):
        return None
    slot.payload[slot.param] = primary_value
    return slot.decide("primary", value=primary_value)


def _rung_prompt_label(slot: _EnumSlot) -> dict | None:
    """The prompt names exactly one option by its label."""
    prompt_value = _option_from_prompt(slot.options, slot.prompt)
    if prompt_value is None or prompt_value not in _option_values(slot.options):
        return None
    slot.payload[slot.param] = prompt_value
    return slot.decide("prompt-label", value=prompt_value)


def _rung_needs_selection(slot: _EnumSlot) -> dict | None:
    """Several options and nothing to choose by — ask, never guess."""
    if len(slot.options) <= 1:
        return None
    return {"terminal": _needs_selection(slot.uri, slot.node, slot.param, slot.cfg,
                                         slot.options, slot.inventory)}


# Resolution ladder for one enum parameter, in precedence order. Each rung
# returns {"decision": …} (resolved), {"terminal": …} (stop the whole flow) or
# None to fall through to the next one.
_ENUM_RUNGS = (
    _rung_skip,
    _rung_result_ref,
    _rung_explicit,
    _rung_single,
    _rung_remembered,
    _rung_primary,
    _rung_prompt_label,
    _rung_needs_selection,
)


def _resolve_slot(slot: _EnumSlot) -> dict:
    """Walk the ladder; an unresolved parameter is reported, not guessed."""
    for rung in _ENUM_RUNGS:
        outcome = rung(slot)
        if outcome is not None:
            return outcome
    return slot.decide("unresolved")


def _enum_params(domains: dict) -> list[tuple[str, dict]]:
    """Route parameters that draw from an environment domain."""
    return [(param, cfg) for param, cfg in domains.items()
            if isinstance(cfg, dict) and cfg.get("type") == "enum" and cfg.get("domain")]


def resolve_env_enums(flow: dict, routes: list[dict], inventories: dict[str, dict] | list[dict],
                      memory: Any = None, prompt: str = "") -> dict:
    """Return ``{ok, flow, decisions}`` or ``needs-selection`` for unresolved env enums."""
    decisions: list[dict] = []
    out_steps: list[dict] = []
    for step in flow.get("steps") or []:
        uri = str(step.get("uri") or "")
        payload = dict(step.get("payload") or {})
        node = _target(uri)
        inventory = _inventory_for(node, inventories)
        for param, cfg in _enum_params(_route_domains(uri, routes)):
            slot = _EnumSlot(
                uri=uri, node=node, param=param, cfg=cfg, payload=payload,
                options=_domain_options(inventory, str(cfg["domain"])),
                inventory=inventory, memory=memory, prompt=prompt,
            )
            outcome = _resolve_slot(slot)
            if "terminal" in outcome:
                return {**outcome["terminal"], "flow": flow}
            decisions.append(outcome["decision"])
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


def resolve_flow_env_enums_with_registry(
    flow: dict,
    routes: list[dict],
    registry: dict,
    *,
    memory: Any = None,
    prompt: str = "",
) -> dict:
    """Like resolve_flow_env_enums but builds inventory from a registry dict."""
    from urirun_flow.flow import _build_env_inventory  # noqa: PLC0415
    return resolve_flow_env_enums(
        flow,
        routes,
        memory=memory,
        prompt=prompt,
        inventory_builder=lambda node: _build_env_inventory(node, registry),
    )
