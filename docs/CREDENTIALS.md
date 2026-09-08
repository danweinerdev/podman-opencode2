# Credentials

[Back to README](../README.md)

Use Podman secrets, exported provider variables, or an interactive provider
login inside OpenCode. Keep credentials out of router and local-provider JSON.

## Add an API key

Run from the project that should receive the key. These examples assume the
launcher is on your `PATH`; in this repository, use `./bin/opencode-container`.

```sh
opencode-container secrets add openai-api-key
```

The command prompts without echoing the key, creates a Podman secret, and adds
its name to the project's `provider_secrets` list. It creates a minimal
`.opencode-sandbox.json` if needed.

You can also supply a file or standard input:

```sh
opencode-container secrets add openai-api-key --in /path/to/key.txt
printf '%s' "$OPENAI_API_KEY" | opencode-container secrets add openai-api-key --in -
```

A secret is available only to projects that list it in `provider_secrets`.
To use an existing secret in another project, add its name to that project's
list; you do not need to create it again.

```json
{
  "schema_version": 1,
  "provider_secrets": ["openai-api-key", "deepseek-api-key"]
}
```

## Remove access

To stop sharing a key with one project, remove its name from that project's
`provider_secrets` list.

To delete the stored secret as well:

```sh
opencode-container secrets remove openai-api-key
```

This deletes the Podman secret and removes the current project's selection.
Other projects referencing that secret will also lose access to it.

## Supported providers

| Secret name | Environment variable |
| --- | --- |
| `openai-api-key` | `OPENAI_API_KEY` |
| `anthropic-api-key` | `ANTHROPIC_API_KEY` |
| `deepseek-api-key` | `DEEPSEEK_API_KEY` |
| `groq-api-key` | `GROQ_API_KEY` |
| `google-api-key` | `GOOGLE_API_KEY` |
| `gemini-api-key` | `GEMINI_API_KEY` |
| `google-generative-ai-api-key` | `GOOGLE_GENERATIVE_AI_API_KEY` |
| `mistral-api-key` | `MISTRAL_API_KEY` |
| `xai-api-key` | `XAI_API_KEY` |
| `openrouter-api-key` | `OPENROUTER_API_KEY` |
| `perplexity-api-key` | `PERPLEXITY_API_KEY` |
| `cohere-api-key` | `COHERE_API_KEY` |
| `together-api-key` | `TOGETHER_API_KEY` |
| `azure-openai-api-key` | `AZURE_OPENAI_API_KEY` |

`openapi-api-key` is also accepted as a compatibility alias for `openai-api-key`.

The launcher automatically forwards these environment variables when set on
the host. A selected Podman secret takes priority over a host variable or an
`env.set` value. Forwarded values are not included in launcher process arguments.
Podman secrets also avoid storing key values in the container's saved environment
configuration. Processes inside the container can still use the supplied keys.

## Saved logins

Interactive provider logins and sessions persist in this project's named volume.
The launcher does not import host OpenCode application state. See
[saved state](SANDBOX.md#saved-state) before moving a project or sharing a volume.
