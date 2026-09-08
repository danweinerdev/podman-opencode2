# Local providers

[Back to README](../README.md)

Connect OpenCode to an OpenAI-compatible model server such as llama.cpp,
Ollama, or LM Studio. Start the server separately; this container does not run it.

## Add your server

Create a machine-wide catalog outside your repositories:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2"
cp examples/local-providers.json.example \
  "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/local-providers.json"
```

Run the copy command from this repository, then edit the destination file.
Here is a minimal catalog for a server on host port `18080`:

```json
{
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local server",
      "options": {
        "baseURL": "http://host.containers.internal:18080/v1"
      },
      "models": {
        "my-model": {
          "name": "My local model",
          "limit": { "context": 32768, "output": 4096 }
        }
      }
    }
  }
}
```

Replace `my-model` with the server's model ID and set limits supported by your
model and server. Adjust the port and API path to match your server.

Use `host.containers.internal` to reach the host. `localhost` inside the
container refers to the container itself. The server must listen on an address
reachable from Podman.

## Select the model

Restart the launcher. The example model appears as `local/my-model` in the model
picker. To assign it to workers, use that same ID in a
[router profile](MODEL_ROUTER.md).

## What happens when the file changes

The launcher validates the catalog on `build`, `run`, and `shell`. At runtime,
it mounts only that file, read-only, as
`/opt/opencode/config/opencode/opencode.json`.

Changes take effect on the next launch. If a project has a `build` block, a
new or changed catalog also triggers a rebuild when its checksum differs from
the image label. Only the checksum goes into the build; the catalog stays on
the host. Without a `build` block, an existing image uses the runtime mount.
Removing the catalog removes the mount on the next launch without a rebuild.

The file must contain a provider-only JSON object (an optional `$schema` is
allowed). Duplicate keys and credential fields are rejected. Keep API keys,
tokens, passwords, and authorization headers out of this file; use
[credentials](CREDENTIALS.md) instead.
