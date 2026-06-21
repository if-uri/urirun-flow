# Contributing to ifURI

Thanks for helping build the **ifURI / urirun** ecosystem.

## Ground rules
- Keep `make test` green and the docs standard passing (`make docs-lint` in `if-uri/ifuri-com`).
- Match the surrounding code and prose style; one logical change per PR; explain the *why*.

## Workflow
1. Branch from `main`.
2. Make the change; update `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com)) and bump `VERSION` when user-facing.
3. Run the repo's `make test`. Sites publish via `make deploy` (see [docs.ifuri.com/repo-standards](https://docs.ifuri.com/repo-standards.html)).
4. Open a PR against `if-uri/<repo>`.

## Connectors
New connector? Scaffold with `make new-connector ID=…` in `if-uri/connect.ifuri.com` and
submit through [connect.ifuri.com](https://connect.ifuri.com). Every connector emits the
`urirun.bindings.v2` contract and documents its `scheme://` URI in the README.

## License
By contributing you agree your work is licensed under this repository's `LICENSE`.
