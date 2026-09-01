import assert from "node:assert/strict"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, resolve } from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

import defaultExport, {
  BAKED_AGENTS,
  DEFAULT_CONFIG,
  applyAgentConfig,
  mergeConfig,
  modelRefFor,
  readModelRouterOverride,
  validateConfig,
  validateOverride,
} from "../index.js"

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..")

/**
 * A minimal in-memory `AgentDraft` that mirrors the `@opencode-ai/plugin/v2`
 * `AgentDraft` interface (list/get/default/update/remove) against a Map of
 * `AgentV2Info`-shaped records. Used to prove `applyAgentConfig` mutates only
 * through the public draft surface.
 */
function makeDraft(agents) {
  const map = new Map(agents.map((agent) => [agent.id, structuredClone(agent)]))
  const defaults = []
  return {
    draft: {
      list: () => [...map.values()],
      get: (id) => map.get(id),
      default: (id) => {
        defaults.push(id)
      },
      update: (id, fn) => {
        const item = map.get(id) ?? agentInfo(id)
        fn(item)
        item.id = id
        map.set(id, item)
      },
      remove: (id) => {
        map.delete(id)
      },
    },
    defaults,
    agent: (id) => map.get(id),
  }
}

function agentInfo(id, overrides = {}) {
  return {
    id,
    mode: id === "orchestrator" ? "primary" : "subagent",
    hidden: false,
    request: { headers: {}, body: {} },
    permissions: [],
    ...overrides,
  }
}

/** A full config mirroring DEFAULT_CONFIG's shape, with the `agents` map. */
function fullConfig() {
  return structuredClone(DEFAULT_CONFIG)
}

async function withEnv(key, value, fn) {
  const prev = process.env[key]
  if (value === undefined) delete process.env[key]
  else process.env[key] = value
  try {
    return await fn()
  } finally {
    if (prev === undefined) delete process.env[key]
    else process.env[key] = prev
  }
}

async function withTempSandbox(t, json) {
  const dir = await mkdtemp(resolve(tmpdir(), "model-router-"))
  t.after(() => rm(dir, { recursive: true, force: true }))
  const path = resolve(dir, "sandbox.json")
  await writeFile(path, typeof json === "string" ? json : JSON.stringify(json))
  return path
}

test("default export is the v2/promise { id, setup } shape", () => {
  assert.equal(typeof defaultExport, "object")
  assert.equal(defaultExport.id, "opencode-model-router")
  assert.equal(typeof defaultExport.setup, "function")
  // The v2/promise contract is exactly { id, setup }: no v1 `server` hook.
  assert.equal("server" in defaultExport, false)
  assert.equal("tui" in defaultExport, false)
})

test("container config registers the router through the native v2 loader", async () => {
  const config = JSON.parse(await readFile(resolve(ROOT, "..", "..", "container-config.json"), "utf8"))
  assert.equal(config.$schema, "./container-config.schema.json")
  assert.equal("plugin" in config, false, "singular plugin selects the legacy v1 loader")
  assert.equal("small_model" in config, false)
  assert.equal("subagent_depth" in config, false)
  assert.equal(Array.isArray(config.skills), true)
  assert.equal(config.skills.includes("/opt/opencode/plugins/model-router/skills"), true)
  assert.equal(config.mcp.servers["code-graph"].disabled, false)
  assert.equal(config.mcp.servers.debug.disabled, false)
  assert.equal(Array.isArray(config.permissions), true)
  assert.equal(Array.isArray(config.plugins), true)
  const entry = config.plugins.find(
    (item) => item?.package === "file:///opt/opencode/plugins/model-router/index.js",
  )
  assert.ok(entry, "native v2 model-router entry is present")
  assert.equal(typeof entry.options, "object")
  assert.equal(entry.options.schema_version, 1)
  assert.deepEqual(Object.keys(entry.options.agents).sort(), [...BAKED_AGENTS].sort())
})

test("container keeps policy assets root-owned and installs only native agents", async () => {
  const source = await readFile(resolve(ROOT, "..", "..", "Containerfile"), "utf8")
  assert.doesNotMatch(source, /opencode-agents|chown -R[^\n]*\/opt\/opencode\/config/)
  assert.doesNotMatch(source, /chmod 1777 \/opt\/opencode\/config\/opencode/)
  assert.match(source, /chmod 0755 \/opt\/opencode\/config\/opencode/)
  assert.doesNotMatch(source, /service\.json/)
  assert.match(source, /COPY plugins\/model-router\/agents\/\*\.md \/opt\/opencode\/config\/opencode\/agent\//)
  assert.match(source, /CMD \["opencode2", "--standalone"\]/)
})

test("baked workers use native v2 permissions and subagent terminology", async () => {
  for (const agent of BAKED_AGENTS) {
    const source = await readFile(resolve(ROOT, "agents", `${agent}.md`), "utf8")
    assert.match(source, /^permissions:/m, `${agent} must use the v2 permissions field`)
    assert.doesNotMatch(source, /^permission:/m, `${agent} must not use the v1 permission field`)
    assert.doesNotMatch(source, /<frugal_result\b/, `${agent} must not require an unused result footer`)
    if (agent !== "orchestrator") {
      const broadRead = source.indexOf('- { action: read, resource: "*", effect: allow }')
      const envAsk = source.indexOf('- { action: read, resource: "*.env", effect: ask }')
      const envVariantAsk = source.indexOf('- { action: read, resource: "*.env.*", effect: ask }')
      const envExampleAllow = source.indexOf('- { action: read, resource: "*.env.example", effect: allow }')
      assert.ok(broadRead >= 0, `${agent} has a broad read rule`)
      assert.ok(envAsk > broadRead, `${agent} restores the .env prompt after broad read`)
      assert.ok(envVariantAsk > envAsk, `${agent} protects .env variants`)
      assert.ok(envExampleAllow > envVariantAsk, `${agent} allows example files last`)
      assert.match(
        source,
        /action: grep, resource: "\*", effect: ask/,
        `${agent} requires approval for grep because it can return protected file contents`,
      )
      assert.doesNotMatch(
        source,
        /action: grep, resource: "\*", effect: allow/,
        `${agent} must not bypass protected reads through the grep tool`,
      )
      assert.doesNotMatch(
        source,
        /action: shell, resource: "(?:cat|head|tail|grep|rg|wc|cmp|diff|git diff|git show|git log|git grep) \*", effect: allow/,
        `${agent} must not bypass protected reads through shell commands`,
      )
      assert.doesNotMatch(
        source,
        /action: shell, resource: "git status", effect: allow/,
        `${agent} must not run repository-configured hooks without approval`,
      )
    }
  }

  const orchestrator = await readFile(resolve(ROOT, "agents", "orchestrator.md"), "utf8")
  assert.match(orchestrator, /`subagent` tool with an `agent`/)
  assert.doesNotMatch(orchestrator, /`task` tool|`subagent_type`/)
  assert.match(orchestrator, /\| `implement_task`\s+\| `implementer`/)
  assert.match(orchestrator, /\| `review_quality`\s+\| `review-quality`/)
  assert.doesNotMatch(orchestrator, /code-implementer|quality-scanner|drift-detector/)
})

test("source does not contain legacy v1 hook or dispatch markers", async () => {
  const source = await readFile(resolve(ROOT, "index.js"), "utf8")
  const forbidden = [
    "tool.execute", // v1 tool interception
    "session.create", // SDK child-session dispatch
    "session.prompt",
    "[model-router:", // placeholder/return-ok relay
    "placeholder",
    "hooksForModels", // v1 hook-builder function
    "pendingDispatch",
    "runDispatch",
    "recordMetric",
    "chat.params",
    "chat.message",
    "chat.headers",
    "tool.definition",
    "experimental.chat",
    "permission.ask",
    "shell.env",
    "command.execute",
  ]
  for (const marker of forbidden) {
    assert.equal(source.includes(marker), false, `source must not contain legacy marker: ${marker}`)
  }
})

test("assigns models across multiple providers (OpenAI + DeepSeek)", () => {
  const config = {
    schema_version: 1,
    profiles: {
      orchestration: { model: "openai/gpt-5.6-sol" },
      reasoning: { model: "deepseek/deepseek-v4-pro" },
      implementation: { model: "deepseek/deepseek-v4-pro" },
      extraction: { model: "openai/gpt-5.6-luna" },
      review: { model: "deepseek/deepseek-v4-flash" },
    },
    agents: {
      orchestrator: "orchestration",
      reasoner: "reasoning",
      implementer: "implementation",
      extractor: "extraction",
      "bulk-researcher": "extraction",
      "bounded-editor": "reasoning",
      "review-blind-spots": "review",
    },
    default_agent: "orchestrator",
  }
  const { draft, agent } = makeDraft(
    [
      "orchestrator",
      "reasoner",
      "implementer",
      "extractor",
      "bulk-researcher",
      "bounded-editor",
      "review-blind-spots",
    ].map((id) => agentInfo(id)),
  )

  applyAgentConfig(draft, config)

  assert.deepEqual(agent("orchestrator").model, { providerID: "openai", id: "gpt-5.6-sol" })
  assert.deepEqual(agent("reasoner").model, { providerID: "deepseek", id: "deepseek-v4-pro" })
  assert.deepEqual(agent("implementer").model, { providerID: "deepseek", id: "deepseek-v4-pro" })
  assert.deepEqual(agent("extractor").model, { providerID: "openai", id: "gpt-5.6-luna" })
  assert.deepEqual(agent("bulk-researcher").model, { providerID: "openai", id: "gpt-5.6-luna" })
  assert.deepEqual(agent("bounded-editor").model, { providerID: "deepseek", id: "deepseek-v4-pro" })
  assert.deepEqual(agent("review-blind-spots").model, { providerID: "deepseek", id: "deepseek-v4-flash" })
})

test("assigns variant and merges request headers/body, leaving unconfigured request empty", () => {
  const config = {
    schema_version: 1,
    profiles: {
      orchestration: {
        model: "openai/gpt-5.6-sol",
        variant: "high",
        request: {
          headers: { "x-routing": "primary" },
          body: { reasoningEffort: "high" },
        },
      },
      reasoning: { model: "deepseek/deepseek-v4-pro", variant: "max" },
      implementation: { model: "deepseek/deepseek-v4-pro" },
      extraction: { model: "openai/gpt-5.6-luna" },
      review: { model: "deepseek/deepseek-v4-flash" },
    },
    agents: DEFAULT_CONFIG.agents,
    default_agent: "orchestrator",
  }
  const { draft, agent } = makeDraft([...BAKED_AGENTS].map((id) => agentInfo(id)))

  applyAgentConfig(draft, config)

  const orchestrator = agent("orchestrator")
  assert.equal(orchestrator.model.variant, "high")
  assert.deepEqual(orchestrator.request.headers, { "x-routing": "primary" })
  assert.deepEqual(orchestrator.request.body, { reasoningEffort: "high" })

  const reasoner = agent("reasoner")
  assert.equal(reasoner.model.variant, "max")
  assert.equal(reasoner.model.providerID, "deepseek")

  // No variant and no request configured -> model has no variant field, and
  // the agent's request remains the untouched empty default.
  const implementer = agent("implementer")
  assert.equal("variant" in implementer.model, false)
  assert.deepEqual(implementer.request, { headers: {}, body: {} })
})

test("merges request fields into a pre-populated agent request without clobbering", () => {
  const config = fullConfig()
  config.profiles.orchestration = {
    model: "openai/gpt-5.6-sol",
    request: { headers: { "x-b": "2" }, body: { b: 2 } },
  }
  const { draft, agent } = makeDraft(
    [...BAKED_AGENTS].map((id) =>
      agentInfo(id, {
        request: { headers: { "x-a": "1" }, body: { a: 1 } },
      }),
    ),
  )

  applyAgentConfig(draft, config)

  assert.deepEqual(agent("orchestrator").request, {
    headers: { "x-a": "1", "x-b": "2" },
    body: { a: 1, b: 2 },
  })
})

test("initializes a missing agent request object before applying request fields", () => {
  const { draft, agent } = makeDraft([agentInfo("orchestrator", { request: undefined })])
  applyAgentConfig(draft, {
    schema_version: 1,
    profiles: {
      orchestration: {
        model: "openai/gpt-5.6-sol",
        request: { headers: { "x-routing": "primary" } },
      },
    },
    agents: { orchestrator: "orchestration" },
  })
  assert.deepEqual(agent("orchestrator").request, {
    headers: { "x-routing": "primary" },
  })
})

test("sets the default agent via draft.default", () => {
  const { draft, defaults } = makeDraft([...BAKED_AGENTS].map((id) => agentInfo(id)))
  applyAgentConfig(draft, fullConfig())
  assert.deepEqual(defaults, ["orchestrator"])
})

test("leaves unmapped agents untouched", () => {
  const { draft, agent } = makeDraft([
    ...BAKED_AGENTS.map((id) => agentInfo(id)),
    agentInfo("some-unmapped-agent"),
    agentInfo("another-unmapped-agent"),
  ])
  applyAgentConfig(draft, fullConfig())
  // No mapping in DEFAULT_CONFIG -> these agents keep no model.
  assert.equal(agent("some-unmapped-agent").model, undefined)
  assert.equal(agent("another-unmapped-agent").model, undefined)
})

test("rejects missing agent definitions before mutating the draft", () => {
  const { draft, agent, defaults } = makeDraft([agentInfo("reasoner")])
  const config = {
    schema_version: 1,
    profiles: { reasoning: { model: "deepseek/deepseek-v4-pro", variant: "high" } },
    agents: { reasoner: "reasoning", ghost: "reasoning" },
    default_agent: "ghost",
  }

  assert.throws(() => applyAgentConfig(draft, config), /agent definition\(s\) not found: ghost/)
  assert.equal(agent("reasoner").model, undefined)
  assert.equal(agent("ghost"), undefined)
  assert.deepEqual(defaults, [])
})

test("routes arbitrary valid agent ids, not a closed role list", () => {
  const config = {
    schema_version: 1,
    profiles: {
      orchestration: { model: "openai/gpt-5.6-sol" },
      custom: { model: "deepseek/deepseek-v4-pro" },
    },
    agents: {
      orchestrator: "orchestration",
      "my-custom-agent": "custom",
      "another.agent_x": "custom",
    },
    default_agent: "orchestrator",
  }
  const { draft, agent } = makeDraft([
    agentInfo("orchestrator"),
    agentInfo("my-custom-agent"),
    agentInfo("another.agent_x"),
  ])
  applyAgentConfig(draft, config)
  assert.deepEqual(agent("my-custom-agent").model, { providerID: "deepseek", id: "deepseek-v4-pro" })
  assert.deepEqual(agent("another.agent_x").model, { providerID: "deepseek", id: "deepseek-v4-pro" })
})

test("accepts a config with no default_agent (optional) and skips draft.default", () => {
  const config = fullConfig()
  delete config.default_agent
  assert.equal(validateConfig(config), config)

  const { draft, defaults } = makeDraft([...BAKED_AGENTS].map((id) => agentInfo(id)))
  applyAgentConfig(draft, config)
  assert.deepEqual(defaults, [])
})

test("rejects a default_agent with no agent mapping", () => {
  const config = fullConfig()
  config.default_agent = "reasoner"
  delete config.agents.reasoner
  assert.throws(() => validateConfig(config), /default_agent "reasoner" has no agent mapping/)
})

test("rejects a default_agent whose agent references a missing profile", () => {
  const config = fullConfig()
  config.agents.orchestrator = "missing"
  assert.throws(() => validateConfig(config), /references missing profile "missing"/)
})

test("rejects structurally invalid configs", () => {
  const missingProfile = fullConfig()
  delete missingProfile.profiles.reasoning
  assert.throws(() => validateConfig(missingProfile), /references missing profile "reasoning"/)

  const badSchema = fullConfig()
  badSchema.schema_version = 2
  assert.throws(() => validateConfig(badSchema), /unsupported schema_version/)

  const emptyProfiles = fullConfig()
  emptyProfiles.profiles = {}
  assert.throws(() => validateConfig(emptyProfiles), /profiles must be a non-empty object/)

  const notObject = "nope"
  assert.throws(() => validateConfig(notObject), /must be an object/)

  const unknownTop = fullConfig()
  unknownTop.extra = true
  assert.throws(() => validateConfig(unknownTop), /unknown field/)

  const badAgentId = fullConfig()
  badAgentId.agents["not a valid id"] = "reasoning"
  assert.throws(() => validateConfig(badAgentId), /invalid agent id/)

  const badModel = fullConfig()
  badModel.profiles.reasoning.model = "no-provider-prefix"
  assert.throws(() => validateConfig(badModel), /provider\/model format/)

  const doubleSlash = fullConfig()
  doubleSlash.profiles.reasoning.model = "a/b/c"
  assert.throws(() => validateConfig(doubleSlash), /provider\/model format/)

  const badVariant = fullConfig()
  badVariant.profiles.reasoning.variant = ""
  assert.throws(() => validateConfig(badVariant), /variant must be a non-empty string/)

  const badRequest = fullConfig()
  badRequest.profiles.reasoning.request = { headers: "nope" }
  assert.throws(() => validateConfig(badRequest), /request.headers must be an object/)
})

test("modelRefFor omits variant when absent and includes it when present", () => {
  assert.deepEqual(modelRefFor({ model: "openai/gpt-5.6-sol" }), {
    providerID: "openai",
    id: "gpt-5.6-sol",
  })
  assert.deepEqual(modelRefFor({ model: "deepseek/deepseek-v4-pro", variant: "max" }), {
    providerID: "deepseek",
    id: "deepseek-v4-pro",
    variant: "max",
  })
})

test("DEFAULT_CONFIG is self-consistent and covers every baked agent", () => {
  assert.equal(validateConfig(DEFAULT_CONFIG), DEFAULT_CONFIG)
  assert.equal(DEFAULT_CONFIG.schema_version, 1)
  assert.equal(DEFAULT_CONFIG.default_agent, "orchestrator")
  // Demonstrates both providers.
  const providers = new Set(
    Object.values(DEFAULT_CONFIG.profiles).map((profile) => profile.model.split("/")[0]),
  )
  assert.ok(providers.has("openai"))
  assert.ok(providers.has("deepseek"))

  // Every baked agent resolves to a defined profile.
  assert.equal(BAKED_AGENTS.length, 10)
  for (const id of BAKED_AGENTS) {
    const profileName = DEFAULT_CONFIG.agents[id]
    assert.equal(typeof profileName, "string", `${id} has a profile mapping`)
    assert.ok(Object.hasOwn(DEFAULT_CONFIG.profiles, profileName), `${id} -> ${profileName} is defined`)
  }
})

test("mergeConfig shallow-merges partial profiles and agents over defaults", () => {
  const override = {
    schema_version: 1,
    profiles: {
      reasoning: { model: "anthropic/claude-opus-4-1", variant: "high" },
      custom: { model: "openai/gpt-5.6-luna" },
    },
    agents: {
      reasoner: "reasoning",
    },
    default_agent: "reasoner",
  }
  const merged = mergeConfig(DEFAULT_CONFIG, override)

  // Overridden profile replaces the base one wholesale (no deep merge).
  assert.deepEqual(merged.profiles.reasoning, { model: "anthropic/claude-opus-4-1", variant: "high" })
  // New profile added alongside the base ones.
  assert.deepEqual(merged.profiles.custom, { model: "openai/gpt-5.6-luna" })
  assert.deepEqual(merged.profiles.orchestration, { model: "openai/gpt-5.6-sol" })
  // Agents merged by id; base entries survive.
  assert.equal(merged.agents.reasoner, "reasoning")
  assert.equal(merged.agents.orchestrator, "orchestration")
  // default_agent overridden.
  assert.equal(merged.default_agent, "reasoner")

  // The merged result is a fully valid config.
  assert.equal(validateConfig(merged), merged)
})

test("mergeConfig keeps base default_agent when the override omits it", () => {
  const merged = mergeConfig(DEFAULT_CONFIG, {
    schema_version: 1,
    agents: { reasoner: "reasoning" },
  })
  assert.equal(merged.default_agent, "orchestrator")
  assert.equal(validateConfig(merged), merged)
})

test("mergeConfig validates the override shape before merging", () => {
  assert.throws(() => mergeConfig(DEFAULT_CONFIG, null), /model_router must be an object/)
  assert.throws(() => mergeConfig(DEFAULT_CONFIG, { schema_version: 2 }), /unsupported schema_version/)
  assert.throws(() => mergeConfig(DEFAULT_CONFIG, { profiles: "nope" }), /profiles must be an object/)
  assert.throws(() => mergeConfig(DEFAULT_CONFIG, { agents: "nope" }), /agents must be an object/)
  assert.throws(() => mergeConfig(DEFAULT_CONFIG, { agents: { "bad id": "x" } }), /invalid agent id/)
  assert.throws(() => mergeConfig(DEFAULT_CONFIG, { default_agent: 42 }), /default_agent must name an agent/)
  assert.throws(() => mergeConfig(DEFAULT_CONFIG, { unknown_key: true }), /unknown field/)
})

test("validateOverride rejects malformed model_router blocks", () => {
  assert.throws(() => validateOverride({ profiles: { "bad name": { model: "a/b" } } }), /invalid profile name/)
  assert.throws(() => validateOverride({ profiles: { p: { model: "no-slash" } } }), /provider\/model format/)
  assert.throws(() => validateOverride({ agents: { a: "" } }), /must name a profile/)
  assert.throws(() => validateOverride({ agents: { "": "x" } }), /invalid agent id/)
  // A partial override with only a default_agent is fine (shape-wise).
  assert.deepEqual(validateOverride({ default_agent: "reasoner" }), { default_agent: "reasoner" })
})

test("a merged override referencing a missing profile is rejected", () => {
  const merged = mergeConfig(DEFAULT_CONFIG, { agents: { reasoner: "does-not-exist" } })
  assert.throws(() => validateConfig(merged), /references missing profile "does-not-exist"/)
})

test("readModelRouterOverride returns undefined when the sandbox has no model_router", async (t) => {
  const path = await withTempSandbox(t, { image: "opencode2:latest", workspace: "." })
  assert.equal(await readModelRouterOverride(path), undefined)
})

test("readModelRouterOverride extracts the model_router block", async (t) => {
  const path = await withTempSandbox(t, {
    model_router: { schema_version: 1, agents: { reasoner: "reasoning" } },
  })
  assert.deepEqual(await readModelRouterOverride(path), {
    schema_version: 1,
    agents: { reasoner: "reasoning" },
  })
})

test("readModelRouterOverride throws on an unreadable path", async () => {
  await assert.rejects(readModelRouterOverride("/definitely/not/here/sandbox.json"), /unable to read model-router config/)
})

test("readModelRouterOverride throws on invalid JSON", async (t) => {
  const path = await withTempSandbox(t, "{ not json")
  await assert.rejects(readModelRouterOverride(path), /invalid JSON in model-router config/)
})

test("readModelRouterOverride throws on a non-object document", async (t) => {
  const path = await withTempSandbox(t, `"a bare string"`)
  await assert.rejects(readModelRouterOverride(path), /must be a JSON object/)
})

test("setup registers DEFAULT_CONFIG when there are no options and no env override", async () => {
  await withEnv("OPENCODE_MODEL_ROUTER_CONFIG", undefined, async () => {
    let transform = null
    let reloads = 0
    const ctx = {
      options: undefined,
      agent: {
        transform: async (fn) => (transform = fn),
        reload: async () => reloads++,
      },
    }
    await defaultExport.setup(ctx)
    assert.equal(typeof transform, "function")
    assert.equal(reloads, 1)

    const { draft, agent } = makeDraft([...BAKED_AGENTS].map((id) => agentInfo(id)))
    await transform(draft)
    assert.deepEqual(agent("orchestrator").model, { providerID: "openai", id: "gpt-5.6-sol" })
    assert.deepEqual(agent("reasoner").model, { providerID: "deepseek", id: "deepseek-v4-pro", variant: "high" })
  })
})

test("setup merges a project override from OPENCODE_MODEL_ROUTER_CONFIG over the defaults", async (t) => {
  const path = await withTempSandbox(t, {
    model_router: {
      schema_version: 1,
      profiles: {
        reasoning: { model: "anthropic/claude-opus-4-1", variant: "high" },
      },
      agents: {
        reasoner: "reasoning",
      },
      default_agent: "reasoner",
    },
  })

  await withEnv("OPENCODE_MODEL_ROUTER_CONFIG", path, async () => {
    let transform = null
    let reloads = 0
    const ctx = {
      options: undefined,
      agent: {
        transform: async (fn) => (transform = fn),
        reload: async () => reloads++,
      },
    }
    await defaultExport.setup(ctx)
    assert.equal(typeof transform, "function")
    assert.equal(reloads, 1)

    const { draft, agent, defaults } = makeDraft(BAKED_AGENTS.map((id) => agentInfo(id)))
    await transform(draft)

    // Overridden profile wins.
    assert.deepEqual(agent("reasoner").model, { providerID: "anthropic", id: "claude-opus-4-1", variant: "high" })
    // Untouched defaults survive.
    assert.deepEqual(agent("orchestrator").model, { providerID: "openai", id: "gpt-5.6-sol" })
    // Overridden default_agent applied.
    assert.deepEqual(defaults, ["reasoner"])
  })
})

test("setup propagates a malformed override as a thrown error", async (t) => {
  const path = await withTempSandbox(t, {
    model_router: { schema_version: 2 },
  })
  await withEnv("OPENCODE_MODEL_ROUTER_CONFIG", path, async () => {
    const ctx = { options: undefined, agent: { transform: async () => {}, reload: async () => {} } }
    await assert.rejects(defaultExport.setup(ctx), /unsupported schema_version/)
  })
})
