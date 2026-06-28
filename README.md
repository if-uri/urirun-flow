# urirun-flow

Author urirun **URI flows** in a typed programming language, and convert them
to/from the canonical YAML flow format — the way **Pydantic** gives data a typed,
validated model that serializes to a schema.

A urirun *flow* is an ordered DAG of URI steps (`query` reads, `command` mutates),
chaining prior results. The interchange format is YAML
(see [examples/17-flows](https://examples.ifuri.com/)); `urirun-flow` lets you build
and validate that flow in code, with autocompletion, and emit the exact YAML a
runner executes.

## Why

YAML is great for sharing and running a flow, but a typed language gives you
**control**: autocompletion of step references, compile-/run-time validation of the
DAG, refactorability, and the ability to compute a flow (loops, conditionals,
parameters) instead of hand-writing YAML. `urirun-flow` is the bridge — round-trip
between the two.

## Use (Python, Pydantic)

```python
from urirun_flow import Flow

flow = Flow(task={"title": "Web recon"}, registry="tools.bindings.json",
            allow=["httpcheck://*", "browser://*", "log://*"])

up   = flow.step("httpcheck://host/url/query/status", id="up", payload={"url": URL})
read = flow.step("browser://chrome/page/query/dom", id="read",
                 payload={"url": URL}, after=[up])
flow.step("log://host/run/command/write", id="audit",
          payload={"detail": read.ref("text")}, after=[read])   # typed reference

print(flow.to_yaml())              # canonical urirun flow YAML
Flow.from_yaml(text)               # parse + validate back into the model
```

`.step()` returns the typed `Step`, so a later step references its output with
`step.ref("field")` — a checked `<id>.<field>` chain rather than a magic string.

The model validates on every build: URIs are well-formed, `depends_on` resolves to a
real step, and the graph is acyclic. `kind` (`query`/`command`) is derived from the URI.

## CLI

```bash
urirun-flow to-yaml web_recon:flow      # import a Python flow object → YAML
urirun-flow validate flow.yaml          # DAG / deps / URIs
urirun-flow from-yaml flow.yaml         # parse + re-emit (normalize / round-trip)
urirun-flow run flow.yaml --execute     # execute through urirun when the runtime is installed
```

The `urirun_flow` import package is owned by this distribution. Typed authoring
and YAML validation need only `urirun-flow`; execution needs the `urirun` runtime
installed as well.

## Proposal: typed flows in any language

The flow is a **language-agnostic contract** (`{task, registry, allow, steps:[{id,
uri, payload, depends_on}]}`). `urirun-flow` is its Python (Pydantic) model; the same
builder→dict mapping is implementable in any typed language and emits the identical
YAML — exactly how the urirun connector SDKs stay in lockstep across languages.

`js/urirun-flow.js` is a runnable JS/TS emitter (typed via `js/urirun-flow.d.ts`) that
builds the **identical** flow contract. `make conformance` builds the same reference
flow in every language and asserts they agree — like `make conformance` for connector
bindings. Verified: **2/2 emitters agree** (Python + JS).

## License
Apache-2.0 — see [LICENSE](LICENSE) / [NOTICE](NOTICE).
