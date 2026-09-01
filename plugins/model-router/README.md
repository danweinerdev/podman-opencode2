# opencode-model-router (v2)

Native v2 model-routing plugin for **OpenCode2**. It is built exclusively on the
`@opencode-ai/plugin/v2/promise` API and uses the `ctx.agent.transform` hook to
assign each agent its own native `AgentV2Info.model` (`ModelRef`) and optional
request headers/body, plus the default agent. The native v2 `subagent` tool then
runs each agent on its assigned model directly.

There is **no** v1 hook function, no `tool.execute.before/after` interception,
no SDK child-session dispatch, and no placeholder/return-ok relay. This is a
deliberate v2-only design: routing is configuration, not interception.

## Module shape

```js
export default { id, setup }   // @opencode-ai/plugin/v2/promise `define(...)`
```

The plugin's config is assembled in two steps:

1. **Defaults** come from the native v2 plugin entry's `options` (see
   `container-config.json`). The entry uses the v2 `plugins` array with
   `{ "package": "...", "options": { ... } }`; the singular `plugin` key is
   the legacy v1 loader and must not be used for this module.
   When no options are supplied, `DEFAULT_CONFIG` is the fallback.
2. **Project overrides** come from the sandbox JSON mounted by the launcher at
   `/run/opencode/sandbox.json`. The plugin reads the path from the
   `OPENCODE_MODEL_ROUTER_CONFIG` environment variable, extracts the top-level
   `model_router` block, and shallow-merges its `profiles`/`agents`/
   `default_agent` over the defaults before validating the merged result.

## Config schema

A **full** config (the plugin defaults, or the merged result):

```json
{
  "schema_version": 1,
  "profiles": {
    "<name>": {
      "model": "provider/model-id",
      "variant": "optional-variant",
      "request": { "headers": {}, "body": {} }
    }
  },
  "agents": { "<agent-id>": "<profile-name>" },
  "default_agent": "orchestrator"
}
```

- `schema_version` must be `1`.
- `profiles` is a non-empty map of profile name → model selection. `model` is
  `provider/model-id`; `variant` and `request` are optional.
- `agents` maps arbitrary valid agent ids to profiles, allowing derived images
  to route additional agent definitions.
- `default_agent` is **optional**; when present it must name an agent that maps
  to a defined profile.

A **partial override** (the `model_router` block of a sandbox config) may carry
only the keys it wants to change — e.g. a single profile, a single agent
mapping, or just `default_agent`. Its entries replace the same-named entries in
the defaults (shallow merge; profile objects are not deep-merged), and
cross-references are validated only after the merge, so an override may point
an agent at a profile defined by the defaults.

Baked profiles demonstrate OpenAI + DeepSeek provider routing; only the
corresponding provider environment variables (`OPENAI_API_KEY`,
`DEEPSEEK_API_KEY`) are required at runtime.

## Testing

```sh
npm install   # generate the lockfile if needed
npm test      # node:test, including a mock AgentDraft
```

The tests prove multi-provider assignment, variant/request merge and defaults,
definition-existence validation, constrained shallow-merge project overrides,
malformed override/path handling, setup registering a merged config, bad-config
rejection, and that the source contains no legacy v1 hook/dispatch markers.
