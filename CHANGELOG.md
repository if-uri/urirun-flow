# Changelog

All notable changes to **urirun-flow** ([Keep a Changelog](https://keepachangelog.com), [SemVer](https://semver.org)).

## [0.2.1]

### Changed
- `urirun-flow` is now the real-source owner of the `urirun_flow` import package and
  the `urirun-flow` console script, instead of a meta-package depending on `urirun`.

## [0.1.0]

### Added
- Typed urirun flow model (Pydantic): `Flow`/`Step`, a fluent builder with `.ref()`
  chaining, DAG/dependency/URI validation, `to_yaml`/`from_yaml` round-trip and a CLI
  (`urirun-flow to-yaml|validate|from-yaml`). TS surface sketch in `examples/flow.ts`.
