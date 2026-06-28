# Changelog

All notable changes to **urirun-flow** ([Keep a Changelog](https://keepachangelog.com), [SemVer](https://semver.org)).

## [0.2.2]

### Changed
- Screenshot flows now keep `screen/query/capture` reachable even when a recalled
  or LLM-generated page-presence `ui/query/verify` fails; the verify becomes
  optional telemetry when it only gates the final capture. The normalization runs
  at the shared `execute_flow` chokepoint, so **recalled episodes** (which replay
  stored steps without re-planning) get it too — not only freshly-planned flows.
- Pure helper modules (`envelope`, `flow_thin`, `flow_verify`) no longer import the
  `urirun` hub runtime just to unwrap envelopes or resolve route targets.
- Flow-local utility helpers (`now_id`, `slug`, `json_write`, `quiet_completion`)
  are now owned by `urirun-flow`, avoiding the historical `urirun.node._util` shim
  for pure flow planning code.
- LLM-backed planning now has an explicit `llm` extra (`litellm>=1.60`) on
  `urirun-flow`.
- Flow routing helpers import the real-source `urirun-connector-router` package
  directly instead of the historical `urirun.node.routing` shim.

## [0.2.1]

### Changed
- `urirun-flow` is now the real-source owner of the `urirun_flow` import package and
  the `urirun-flow` console script, instead of a meta-package depending on `urirun`.

## [0.1.0]

### Added
- Typed urirun flow model (Pydantic): `Flow`/`Step`, a fluent builder with `.ref()`
  chaining, DAG/dependency/URI validation, `to_yaml`/`from_yaml` round-trip and a CLI
  (`urirun-flow to-yaml|validate|from-yaml`). TS surface sketch in `examples/flow.ts`.
