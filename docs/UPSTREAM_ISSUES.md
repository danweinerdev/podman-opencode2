# Upstream issues

Behaviors of the runtime this image runs that are not ours to fix, recorded so a
doc wording or a workaround carries its reason. Each entry names the version it was
measured on. Re-check an entry after a runtime upgrade: the dispatch integration
test (`tests/model-router-dispatch.test.py`) fails when the behavior below
changes, which is the prompt to update or delete the entry.

Measured on `opencode2 v0.0.0-beta-19234`, the version baked into the image as of
2026-09-25, with providers declared through `@ai-sdk/openai-compatible`.

## Agent `request` options never reach an openai-compatible provider

**What the config asks for.** A model selection may carry
`request: { "headers": {...}, "body": {...} }`, described elsewhere as options sent
with every request for that model.

**What happens.** The router writes them onto the agent and `GET /api/agent` shows
them, but the provider receives neither the headers nor the body keys. The same
request does carry the provider's own `options.headers` from the opencode config,
so this is not a broken HTTP path or an SDK rejecting unknown fields: the agent's
`request` object is dropped between the agent record and the provider call.

**Evidence.** Control, from a server on the shipped image with a router config that
sets one header and one body key per agent:

```
orchestrator  model=fake/lane-model
              request={"headers":{"x-router-route":"..."},"body":{"router_marker":"..."}}
```

Wire, recorded by a mock provider standing behind that same provider id, five
requests covering a parent turn and a dispatched child:

```
body keys: model, messages, tools, prompt_cache_key, store, stream, stream_options
x-router-route: absent      router_marker: absent
x-provider-fixed: "provider-options"      (the provider's own options.headers)
```

**Reproducing it.** Start a container from the dev image with a generated opencode
config mounted at `/opt/opencode/config/opencode/opencode.json` declaring an
`@ai-sdk/openai-compatible` provider pointed at a recording mock, and a router file
whose selection sets `request`. Then `opencode2 serve`, create a session on that
provider and model, and read what the mock logged.

One trap: do not pass `OPENCODE_CONFIG=/path/to/your/config`. The image bakes
`OPENCODE_CONFIG=/etc/opencode/container-config.json`, which is what registers the
model-router plugin, so overriding it unloads the router and every agent reports
`model: null` with an empty `request`. That looks identical to "the router did not
apply my options" and it is not: check that the agent record carries the markers
before believing anything about the wire.

**Scope of the finding.** Measured for `@ai-sdk/openai-compatible` on the version
above, which is how `bin/opencode-container` declares every local provider. Other
adapters, and `variant` propagation, were not measured and are not claimed here.

**Consequences in this repository.**

- `plugins/model-router/README.md` and `docs/MODEL_ROUTER.md` say what arrives
  rather than implying the options take effect.
- The router keeps accepting and merging `request`. It is correct per schema, it is
  what a fixed runtime will need, and dropping the field would be a config break
  for a bug that is not ours.
- `tests/model-router-dispatch-driver.py` asserts the markers are absent while
  asserting the provider-level header is present, so the pin cannot pass by
  accident.

**When this is fixed upstream.** The driver assertion fails. Invert it to require
`route_header` and `route_body` on the relevant requests, drop the caveat wording
from both docs, and delete this entry.

## Unknown agent frontmatter keys are copied into the agent's request body

**What happens.** An agent defined by markdown registers correctly whether or not
its frontmatter holds a key the runtime knows. Once one unknown key is present, the
raw frontmatter is also copied into that agent's `request.body`, including keys the
runtime understands (`permissions` alongside a custom key, measured). A file with
only known keys keeps an empty body.

```
review-adr  request.body = {"review-gates": [...], "permissions": [...]}   # unknown key present
review-quality request.body = {}                                           # known keys only
```

**Why it is recorded rather than fixed here.** Given the entry above, that body
never reaches a provider on this runtime, so the effect is confined to what
`GET /api/agent` reports. It matters if the first entry is fixed: a project agent
declaring `review-gates` in `.agents` frontmatter would then start sending that
key, plus its permission rules, to the provider.

**Consequences in this repository.** `docs/SANDBOX.md` keeps the gate declaration
in frontmatter, where it is structured and next to the lane it describes. If a
runtime upgrade forwards body params, move that declaration out of frontmatter
before it goes on the wire, and re-run the launcher's gate-declaration tests.
