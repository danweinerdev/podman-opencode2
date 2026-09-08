# OpenCode2 Container

Run OpenCode2 in a Podman container with code navigation, debugging, planning,
and model routing ready to use. Your project is mounted into the container;
logins and sessions are saved in a separate volume for each project.

## Capabilities

- **Code navigation:** explore symbols, callers, and dependencies with code-graph.
- **Debugging:** use the debug MCP server and LLDB.
- **Planning:** write specs, plan work, and review implementation with the SDD planner.
- **Model routing:** assign different models to implementation, research, and review agents.
- **Local models:** connect to OpenAI-compatible servers, including llama.cpp, Ollama, and LM Studio.
- **Consistent environment:** use a Fedora-based runtime with bundled tools and skills.
  The development image adds Python tooling and a container client.

This project uses a pinned **OpenCode2 preview**. Version pins live in the
[Containerfile](Containerfile).

## Quick start

You need Linux, working Podman, Python 3.10+, Git, and Make. Run these commands
from this repository's root.

1. Build the runtime and development images:

   ```sh
   make build
   ```

2. Add credentials for the default OpenAI and DeepSeek routes. Each command
   prompts for a key without echoing it:

   ```sh
   ./bin/opencode-container secrets add openai-api-key
   ./bin/opencode-container secrets add deepseek-api-key
   ```

   Already exporting `OPENAI_API_KEY` and `DEEPSEEK_API_KEY`? The launcher
   forwards them automatically. For other models, see [model routing](docs/MODEL_ROUTER.md)
   and [local providers](docs/LOCAL_PROVIDERS.md).

3. Enable the host Podman socket and launch:

   ```sh
   systemctl --user start podman.socket
   ./bin/opencode-container
   ```

   This repository enables host container access in `.opencode-sandbox.json`.
   It lets the sandbox control your host container engine. If you don't need
   that capability, set `containers` to `false` and skip the socket command.

Select your primary model in OpenCode. Worker agents use their configured routes.

## Everyday commands

```sh
./bin/opencode-container               # start OpenCode for this project
./bin/opencode-container shell         # open a shell in the container
./bin/opencode-container run CMD       # run another command
./bin/opencode-container build --force # rebuild the configured image
```

The launcher uses the directory you run it from to find project settings and
saved state. See [setup](docs/SETUP.md) to use it with another repository.

## Guides

| I want to… | Read |
| --- | --- |
| Set up another project or fix a launch problem | [Setup](docs/SETUP.md) |
| Connect a local model server | [Local providers](docs/LOCAL_PROVIDERS.md) |
| Choose models for agents | [Model router](docs/MODEL_ROUTER.md) |
| Manage API keys and saved logins | [Credentials](docs/CREDENTIALS.md) |
| Change mounts, environment, persistence, or container access | [Sandbox configuration](docs/SANDBOX.md) |
| Build custom images or run checks | [Development](docs/DEVELOPMENT.md) |
