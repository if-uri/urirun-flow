// flow.ts — the TypeScript surface for typed urirun flows (sketch mirroring the
// Python Pydantic model). Builds the same language-agnostic flow contract and
// emits the identical YAML, so TS/JS authors get the same typed control.
export interface Step { id: string; uri: string; operation?: string; kind?: string;
  payload?: Record<string, unknown>; depends_on?: string[]; }

export class Flow {
  task: Record<string, unknown>; registry?: string; allow: string[]; steps: Step[] = [];
  constructor(o: { task?: Record<string, unknown>; registry?: string; allow?: string[] } = {}) {
    this.task = o.task ?? {}; this.registry = o.registry; this.allow = o.allow ?? [];
  }
  step(uri: string, o: { id?: string; payload?: Record<string, unknown>; after?: (Step | string)[] } = {}): Step {
    if (!/^[a-z][a-z0-9+.-]*:\/\//.test(uri)) throw new Error(`not a URI: ${uri}`);
    const s: Step = { id: o.id ?? `s${this.steps.length + 1}`, uri,
      payload: o.payload, depends_on: (o.after ?? []).map(a => typeof a === "string" ? a : a.id) };
    this.steps.push(s); return s;
  }
  toDict() {
    const out: Record<string, unknown> = {};
    if (Object.keys(this.task).length) out.task = this.task;
    if (this.registry) out.registry = this.registry;
    if (this.allow.length) out.allow = this.allow;
    out.steps = this.steps.map(s => ({ id: s.id, uri: s.uri,
      ...(s.payload && Object.keys(s.payload).length ? { payload: s.payload } : {}),
      ...(s.depends_on && s.depends_on.length ? { depends_on: s.depends_on } : {}) }));
    return out;
  }
}
export const ref = (s: Step, field = "") => field ? `${s.id}.${field}` : s.id;
