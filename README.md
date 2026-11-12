# OpenCode2 Container

Run OpenCode2 in a Podman container with code navigation, debugging, planning,
and model routing ready to use. Your project is mounted into the container;
logins and sessions are saved in a separate volume for each project.

## Scope

This project sandboxes OpenCode in a rootless Podman container, prepares that
environment (baked runtime, tools, MCP servers, ai-devtools) and presents it to
the user (launcher, mounts, config).

## Capabilities

- **Code navigation:** explore symbols, callers, and dependencies with code-graph.
- **Debugging:** use the debug MCP server and LLDB.
- **Planning:** work through specs, build, and review with living PRDs and
  specialist agents from ai-devtools.
- **Model routing:** assign models to the ai-devtools agents with `aid.json`.
- **Local models:** connect to OpenAI-compatible servers, including llama.cpp, Ollama, and LM Studio.
- **Consistent environment:** use a Fedora-based runtime with bundled tools and skills.
  The development image adds Python tooling and `make`.
- **Containers:** by default the sandbox runs its own rootless Podman engine with
  no host access; set `containers` to `"host"` to use the host socket instead; see
  [container access](docs/SANDBOX.md#container-access).

This project pins **OpenCode2 2.2.6**. Version pins live in the
[Containerfile](Containerfile).

## Quick start

You need Linux, working Podman, Python 3.10+, Git, and Make. Run these commands
from this repository's root.

1. Build the runtime and development images:

   ```sh
   make build
   ```

2. Add credentials for the models you use. These examples add OpenAI and
   DeepSeek keys (the image's title agent uses OpenAI); each command prompts for
   a key without echoing it:

   ```sh
   ./bin/opencode-container secrets add openai-api-key
   ./bin/opencode-container secrets add deepseek-api-key
   ```

   Already exporting `OPENAI_API_KEY` and `DEEPSEEK_API_KEY`? The launcher
   forwards them automatically. For other models, see
   [model routing](docs/SETUP.md#model-routing) and
   [local providers](docs/LOCAL_PROVIDERS.md).

3. Launch:

   ```sh
   ./bin/opencode-container
   ```

   The default `containers` setting is `"nested"`: an engine inside the sandbox
   with no host access, so no host socket is needed. Set it to `"host"` in
   `.opencode-sandbox.json` to use the host's container engine, or `false` for
   none.

Select your primary model in OpenCode. Agents mapped in `aid.json` use their
profile; the rest follow the session model.

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
| Choose models for agents | [Model routing](docs/SETUP.md#model-routing) |
| Manage API keys and saved logins | [Credentials](docs/CREDENTIALS.md) |
| Change mounts, environment, persistence, or container access | [Sandbox configuration](docs/SANDBOX.md) |
| Build custom images or run checks | [Development](docs/DEVELOPMENT.md) |
