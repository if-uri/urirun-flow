"""A typed urirun flow that emits the same YAML as examples/17-flows/web-recon.flow.yaml."""
from urirun_flow import Flow

URL = "https://example.com"


def build() -> Flow:
    flow = Flow(
        task={"title": "Web recon — is it up, read the page, log it"},
        registry="tools.bindings.json",
        allow=["httpcheck://*", "browser://*", "log://*", "time://*"],
    )
    up = flow.step("httpcheck://host/url/query/status", id="up", payload={"url": URL})
    read = flow.step("browser://chrome/page/query/dom", id="read",
                     payload={"url": URL, "max": 400}, after=[up])
    flow.step("log://host/run/command/write", id="audit",
              payload={"event": "recon", "detail": read.ref("text")}, after=[read])
    return flow


flow = build()

if __name__ == "__main__":
    print(flow.to_yaml())
