// urirun-flow (JS) — author urirun URI flows in JS/TS and emit the canonical flow
// contract (the same `{task, registry, allow, steps}` shape the Python model emits).
// UMD: CommonJS / ES module / browser global. Types in urirun-flow.d.ts.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.UrirunFlow = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  var URI_RE = /^[a-z][a-z0-9+.-]*:\/\//;

  function Flow(o) {
    o = o || {};
    this.task = o.task || {};
    this.registry = o.registry;
    this.allow = o.allow || [];
    this.steps = [];
  }

  Flow.prototype.step = function (uri, o) {
    o = o || {};
    if (!URI_RE.test(uri)) throw new Error("not a URI: " + uri);
    var s = {
      id: o.id || "s" + (this.steps.length + 1),
      uri: uri,
      operation: o.operation,
      kind: o.kind,
      payload: o.payload || {},
      depends_on: (o.after || []).map(function (a) { return typeof a === "string" ? a : a.id; }),
    };
    if (s.kind == null) {
      var segs = uri.split("://")[1].split("/");
      ["query", "command", "assertion"].some(function (k) { if (segs.indexOf(k) >= 0) { s.kind = k; return true; } });
    }
    this.steps.push(s);
    this._validate();
    return s;
  };

  Flow.prototype._validate = function () {
    var ids = this.steps.map(function (s) { return s.id; });
    if (new Set(ids).size !== ids.length) throw new Error("duplicate step ids");
    var known = new Set(ids), graph = {};
    this.steps.forEach(function (s) {
      graph[s.id] = s.depends_on || [];
      s.depends_on.forEach(function (d) { if (!known.has(d)) throw new Error("step " + s.id + " depends on unknown " + d); });
    });
    var state = {};
    (function () {
      function visit(n) {
        if (state[n] === 1) throw new Error("dependency cycle through " + n);
        if (state[n] === 2) return;
        state[n] = 1; (graph[n] || []).forEach(visit); state[n] = 2;
      }
      Object.keys(graph).forEach(visit);
    })();
  };

  // canonical flow contract — must match the Python Flow.to_dict()
  Flow.prototype.toDict = function () {
    var out = {};
    if (Object.keys(this.task).length) out.task = this.task;
    if (this.registry) out.registry = this.registry;
    if (this.allow.length) out.allow = this.allow;
    out.steps = this.steps.map(function (s) {
      var e = { id: s.id, uri: s.uri };
      if (s.operation) e.operation = s.operation;
      if (s.payload && Object.keys(s.payload).length) e.payload = s.payload;
      if (s.depends_on && s.depends_on.length) e.depends_on = s.depends_on;
      return e;
    });
    return out;
  };

  function ref(step, field) { return field ? step.id + "." + field : step.id; }

  return { Flow: Flow, ref: ref };
});
