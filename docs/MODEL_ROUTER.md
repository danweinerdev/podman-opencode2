# Model router

[Back to README](../README.md)

The router chooses a model for each agent. A **profile** names a model and its
options; an **agent mapping** assigns that profile to an agent.

Your default agent follows the model selected in the OpenCode UI. Worker agents
use their profiles. Set `pin_default_agent_model` to `true` to make the default
agent use its profile too.

## Set models for all projects

From this repository, copy the example to your user config directory:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2"
cp examples/model-router.json.example \
  "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/model-router.json"
```

Edit that file to change only the routes you need. For example:

```json
{
  "schema_version": 1,
  "profiles": {
    "local-coding": { "model": "local/my-model" }
  },
  "agents": {
    "implementer": "local-coding",
    "bounded-editor": "local-coding"
  }
}
```

This example uses the provider from [local providers](LOCAL_PROVIDERS.md).
Hosted models use the same `provider/model-id` format.

## Override one project

Add a `model_router` block to the project's `.opencode-sandbox.json`:

```json
{
  "schema_version": 1,
  "model_router": {
    "profiles": {
      "coding": { "model": "local/my-model" }
    },
    "agents": { "engineer": "coding" },
    "default_agent": "engineer",
    "pin_default_agent_model": true
  }
}
```

Restart the launcher to apply routing changes. No image rebuild is needed.
Keep credentials out of router files; use [provider credentials](CREDENTIALS.md).

## Which settings win?

Settings apply in this order, with later settings taking priority:

1. Baked defaults in [container-config.json](../container-config.json).
2. Your user-wide `opencode2/model-router.json`.
3. The project's `model_router` block.

You can override individual profiles and agent mappings. Replacing a profile
replaces its whole object, including any `variant` or `request` options.
Omitted settings keep their previous values.

Each layer must be valid after it is applied. A project override cannot fix an
invalid global mapping. A mapping must name an existing agent; it does not
create one. Custom images can supply additional agent definitions.

## Available agents

| Agent | Role |
| --- | --- |
| `orchestrator` | Coordinate work across agents; the default primary agent |
| `engineer` | Investigate, implement, and verify in one context; another primary agent |
| `reasoner` | Work through complex decisions |
| `extractor` | Extract focused information |
| `bulk-researcher` | Gather information across a larger scope |
| `bounded-editor` | Make a limited edit |
| `implementer` | Implement assigned work |
| `review-plan-drift` | Check alignment with the plan |
| `review-quality` | Review code quality |
| `review-spec-compliance` | Check requirements |
| `review-blind-spots` | Look for missed risks and gaps |

The baked routes use OpenAI and DeepSeek. See
[container-config.json](../container-config.json) for the exact model assignments.
Agents have their own prompts and permissions. SDD uses these same workers.

## Plugin details

This is a native v2 plugin: OpenCode's `subagent` tool runs workers directly on
their assigned models. The router does not intercept tool calls or enforce
runtime guards such as web-fetch limits or sensitive-data scrubbing.
Permissions and agent prompts govern those behaviors.

For profile request options, v2 hooks, and validation details, see the
[plugin reference](../plugins/model-router/README.md).
