# opencode-model-router (v2)

Native v2 model-routing plugin for **OpenCode2**. It is built exclusively on the
`@opencode-ai/plugin/v2/promise` API and uses the `ctx.agent.transform` hook to
assign worker agents native `AgentV2Info.model` (`ModelRef`) values and optional
request headers/body, plus the default agent. The native v2 `subagent` tool then
runs each worker on its assigned model directly.

There is **no** v1 hook function, no `tool.execute.before/after` interception,
no SDK child-session dispatch, and no placeholder/return-ok relay. This is a
deliberate v2-only design: routing is configuration, not interception.

## Module shape

```js
export default { id, setup }   // @opencode-ai/plugin/v2/promise `define(...)`
```

The plugin's config is assembled in three layers, in increasing precedence:

1. **Defaults** come from the native v2 plugin entry's `options` (see
   `container-config.json`). The entry uses the v2 `plugins` array with
   `{ "package": "...", "options": { ... } }`; the singular `plugin` key is
   the legacy v1 loader and must not be used for this module.
   When no options are supplied, `DEFAULT_CONFIG` is the fallback.
2. **User-global overrides** come from the standalone partial config discovered
   on the host at
   `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/model-router.json`. The launcher
   mounts that exact file (not its parent directory) read-only at
   `/run/opencode/model-router-global.json` and sets
   `OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG`. The plugin shallow-merges this layer
   over the defaults and validates the merged result immediately, so a project
   override cannot repair invalid global cross-references.
3. **Project overrides** come from the sandbox JSON mounted by the launcher at
   `/run/opencode/sandbox.json`. The plugin reads the path from the
   `OPENCODE_MODEL_ROUTER_CONFIG` environment variable, extracts the top-level
   `model_router` block, and shallow-merges its `models`/`agents`/
   `default_agent` over the global result before validating the final result.
   The workspace-only environment variable remains backward compatible when no
   global config is present.

Both override files are runtime inputs. Editing either requires only restarting
the launcher/container, not rebuilding the image. Do not put API keys,
authorization headers, tokens, passwords, or other credentials in model-router
config; use Podman secrets or supported provider environment variables.

## Config schema

A **full** config (the plugin defaults, or the merged result):

```json
{
  "schema_version": 2,
  "models": {
    "<name>": {
      "model": "provider/model-id",
      "variant": "optional-variant",
      "request": { "headers": {}, "body": {} }
    }
  },
  "agents": {
    "<agent-id>": "<model-name>",
    "<other-agent>": { "model": "provider/model-id", "variant": "optional-variant" }
  },
  "default_agent": "orchestrator",
  "pin_default_agent_model": false
}
```

- `schema_version` must be `2`. A config that still carries `profiles` — v1's
  name for `models` — is refused with the rename spelled out; nothing is
  translated on the user's behalf.
- `agents` is the map that decides routing. Keys are arbitrary valid agent ids: a
  baked agent, a project's `.agents` definition, or one a derived image installs.
  Each value is either a name in `models` or an inline selection, so an agent that
  wants its own model needs one entry and no shared declaration.
- `models` is optional shared storage for selections several agents want. In both
  forms `model` is `provider/model-id`, and `variant` and `request` are optional;
  `request` adds `headers` and `body` to that agent's requests.
- `default_agent` is **optional**; when present it must name an agent whose
  selection resolves.
- `pin_default_agent_model` is optional and defaults to `false`, which keeps
  selecting the default agent while leaving its model under OpenCode's
  persisted UI preference. Set it to `true` to route that agent through its model
  too. Request options still apply in either mode. Before a UI model has
  been selected, OpenCode uses its provider fallback.

A **partial override** (either the standalone user-global document or the
`model_router` block of a sandbox config) may carry only the keys it wants to
change — one shared model, one agent's selection, or just
`default_agent`/`pin_default_agent_model`. Its entries replace the same-named
entries in lower-precedence layers (shallow merge; a shared model and an inline
selection are each replaced whole, never deep-merged), so a project can move one
agent off a shared model onto its own, or the reverse, without disturbing the
rest. An override may point an agent at a model defined by a lower layer. Global
cross-references are validated after merging over defaults, before the workspace
layer; final cross-references are validated again after the workspace merge.

Baked models demonstrate OpenAI + DeepSeek provider routing; only the
corresponding provider environment variables (`OPENAI_API_KEY`,
`DEEPSEEK_API_KEY`) are required at runtime. The launcher refuses a file in the
retired shape before it starts a container, because the plugin's own rejection
looks different from outside: the plugin is disabled, every agent follows the
session model, and one log line is the only trace.

## Testing

```sh
npm install   # generate the lockfile if needed
npm test      # node:test, including a mock AgentDraft
```

The tests prove multi-provider assignment, variant/request merge and defaults,
definition-existence validation, both target forms, three-layer shallow-merge precedence,
independent global validation, malformed override/path handling, setup
registering a merged config, bad-config rejection, and that the source contains
no legacy v1 hook/dispatch markers.
