# Changelog

All notable changes to **urirun-flow** ([Keep a Changelog](https://keepachangelog.com), [SemVer](https://semver.org)).

## [0.1.0]

### Added
- Typed urirun flow model (Pydantic): `Flow`/`Step`, a fluent builder with `.ref()`
  chaining, DAG/dependency/URI validation, `to_yaml`/`from_yaml` round-trip and a CLI
  (`urirun-flow to-yaml|validate|from-yaml`). TS surface sketch in `examples/flow.ts`.
