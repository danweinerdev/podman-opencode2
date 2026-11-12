# Upstream issues

Behaviors of the runtime this image runs that are not ours to fix, recorded so a
doc wording or a workaround carries its reason. They were measured on
`opencode v2.0.22`; the config side of the first entry was re-checked on the
runtime now baked (`2.2.6`). Provider delivery remains unmeasured on `2.2.6`.
Those measurements declared providers through `@ai-sdk/openai-compatible`.

## Unknown agent frontmatter keys are copied into the agent's request body

**What happens.** An agent defined by markdown registers correctly whether or not
its frontmatter holds a key the runtime knows. Once one unknown key is present, the
raw frontmatter is also copied into that agent's `request.body`. On `2.2.6` a
project agent carrying `review-gates` reports:

```
review-adr    request.body = {"review-gates": [...]}   # unknown key present
review-format request.body = {}                        # known keys only
```

(On `2.0.22` a `permissions` array alongside the custom key also appeared in the
body; on `2.2.6` the array form was measured to leave `request.body` empty, so the
copy reproduces without a native `permissions` array.)

**Why it is recorded rather than fixed here.** On `2.2.6` the config side is
confirmed: `debug agents` reports the unknown key in the agent's `request.body`.
Whether a provider ever receives that body was measured not to happen on `2.0.22`
and is still unmeasured on `2.2.6`. If provider delivery forwards body params, a
project agent declaring `review-gates` in `.agents` frontmatter would start sending
that key to the provider.

**Consequences in this repository.** `docs/SANDBOX.md` keeps the gate declaration
in frontmatter, where it is structured and next to the lane it describes. If a
runtime upgrade forwards body params, move that declaration out of frontmatter
before it goes on the wire, and re-run the launcher's gate-declaration tests.
