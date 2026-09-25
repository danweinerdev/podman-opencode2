# Model router

[Back to README](../README.md)

The router chooses a model for each agent. `agents` is that map: every agent
names what it runs on, either as its own model selection written inline or as the
name of a shared entry in `models`.

Your default agent follows the model selected in the OpenCode UI. Worker agents
use the model you name for them. Set `pin_default_agent_model` to `true` to make the default
agent use the model named for it too.

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
  "schema_version": 2,
  "models": {
    "local-coding": { "model": "local/my-model" }
  },
  "agents": {
    "implementer": "local-coding",
    "bounded-editor": "local-coding"
  }
}
```

Two workers want the same model here, so it is named once. One agent wanting its
own model needs no `models` entry at all:

```json
{
  "schema_version": 2,
  "agents": {
    "review-adr": { "model": "local/my-model", "variant": "high" }
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
    "models": {
      "deep-review": { "model": "deepseek/deepseek-v4-pro", "variant": "high" }
    },
    "agents": {
      "engineer": { "model": "local/my-model" },
      "review-adr": "deep-review"
    },
    "default_agent": "engineer",
    "pin_default_agent_model": true
  }
}
```

The engineer runs on a model only it uses, so the selection is written inline;
`review-adr`, a reviewer this project added in `.agents`, shares one. Both forms
can appear in the same block, and either may come from any layer.

Restart the launcher to apply routing changes. No image rebuild is needed.
Keep credentials out of router files; use [provider credentials](CREDENTIALS.md).

## Which settings win?

Settings apply in this order, with later settings taking priority:

1. Baked defaults in [container-config.json](../container-config.json).
2. Your user-wide `opencode2/model-router.json`.
3. The project's `model_router` block.

You can override individual models and agent entries. Replacing either replaces
its whole object, including any `variant` or `request` options, so one agent can
be moved off a shared model onto its own inline selection, or the reverse, without
touching the others. Omitted settings keep their previous values.

Each layer must be valid after it is applied. A project override cannot fix an
invalid global entry. An entry must name an existing agent; it does not create
one. Projects (`.agents`) and derived images supply the additional definitions.

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

A project adds its own agents in `.agents`; name one under `model_router.agents`
and give it a model inline, which is what the inline form is for. An agent that is
not named here inherits the session model, and an id named here that never arrives
disables routing for every agent, so check the spelling against the directory
listing. See [project agent definitions](SANDBOX.md#project-agent-definitions).

## Upgrading from the previous shape

`profiles` is now `models`, and an `agents` value may be that shared name or the
model selection itself. The version is 2, and version 1 is refused rather than
read half-way: a launcher pointed at a `profiles` file stops with a message naming
the rename, because the alternative is a disabled router and every agent quietly
following the session model.

## Plugin details

This is a native v2 plugin: OpenCode's `subagent` tool runs workers directly on
their assigned models. The router does not intercept tool calls or enforce
runtime guards such as web-fetch limits or sensitive-data scrubbing.
Permissions and agent prompts govern those behaviors.

Request options are the one part of a selection the shipped runtime does not act
on. `request.headers` and `request.body` are validated, merged, and attached to
the agent, but nothing forwards them to a provider declared with
`@ai-sdk/openai-compatible`, which is how [local providers](LOCAL_PROVIDERS.md)
are declared here. Put a header that must reach such a provider in that provider's
own `options.headers` instead. The dispatch test pins this behavior; see
[upstream issues](UPSTREAM_ISSUES.md).

For request options on a model, v2 hooks, and validation details, see the
[plugin reference](../plugins/model-router/README.md).
