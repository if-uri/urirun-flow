const { Flow, ref } = require("../js/urirun-flow.js");
const URL = "https://example.com";
const f = new Flow({ task: { title: "Web recon" }, registry: "tools.bindings.json",
                     allow: ["httpcheck://*", "browser://*", "log://*"] });
const up = f.step("httpcheck://host/url/query/status", { id: "up", payload: { url: URL } });
const read = f.step("browser://chrome/page/query/dom", { id: "read", payload: { url: URL }, after: [up] });
f.step("log://host/run/command/write", { id: "audit",
        payload: { detail: ref(read, "text") }, after: [read] });
process.stdout.write(JSON.stringify(f.toDict()));
