# TODO

- [ ] Move more runtime-only imports behind call sites so `urirun_flow.flow` can be imported
      without the full `urirun` runtime when only static planning helpers are needed.
- [ ] Split pure flow model/conformance tests from runtime execution tests in CI.
- [ ] Ship the TS package (`@urirun/flow`).
