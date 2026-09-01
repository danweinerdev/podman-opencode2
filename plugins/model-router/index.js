/**
 * opencode-model-router — native v2 plugin for OpenCode2.
 *
 * This plugin is intentionally v2-only. It uses the
 * `@opencode-ai/plugin/v2/promise` API (`default export { id, setup }`) and the
 * agent-transform hook to assign a native `AgentV2Info.model` `ModelRef` (and
 * optional request headers/body) to each agent, keyed by agent id. The native
 * v2 `task` tool then uses the selected agent's model directly, so this module
 * performs no tool-execution interception (no execute-before/after hooks), no
 * SDK child-session relay, and no decoy/return-ok flow.
 *
 * Config resolution: the baked plugin tuple `options` provide the full default
 * config. When `OPENCODE_MODEL_ROUTER_CONFIG` names a launcher-mounted sandbox
 * JSON, the plugin reads its top-level `model_router` block and shallow-merges
 * that block's `profiles`/`agents`/`default_agent` over the defaults, then
 * validates the merged result. Routing is by arbitrary valid agent id — there
 * is no closed role list.
 *
 * Exported for tests:
 *   - `validateConfig(value)`        full merged-config validation (throws)
 *   - `validateOverride(value)`      partial `model_router` shape validation
 *   - `mergeConfig(base, override)`  shallow profile/agent merge
 *   - `readModelRouterOverride(path)` read + parse a sandbox JSON file
 *   - `modelRefFor(profile)`         profile -> { providerID, id, variant? }
 *   - `applyAgentConfig(draft, config)`  pure draft application
 *   - `BAKED_AGENTS` and `DEFAULT_CONFIG`
 */

import { readFile } from "node:fs/promises"

import { define } from "@opencode-ai/plugin/v2/promise"

export const SCHEMA_VERSION = 1

/**
 * Agent ids the container bakes as OpenCode agent definitions. The
 * model-router plugin assigns each a model via the `agents` map. Routing is by
 * arbitrary valid agent id, so this list is documentation/test coverage only,
 * never a validation gate.
 */
export const BAKED_AGENTS = Object.freeze([
  // native v2 orchestration / implementation / review workers
  "orchestrator",
  "reasoner",
  "extractor",
  "bulk-researcher",
  "bounded-editor",
  "implementer",
  "review-plan-drift",
  "review-quality",
  "review-spec-compliance",
  "review-blind-spots",
  // sdd-planner agents
  "researcher",
  "plan-reviewer",
  "code-implementer",
  "quality-scanner",
  "spec-reviewer",
  "spec-compliance",
  "drift-detector",
  "blind-spot-finder",
])

/**
 * Bundled fallback configuration. Demonstrates OpenAI + DeepSeek provider
 * routing: orchestration/extraction run on OpenAI, reasoning/implementation/
 * review on DeepSeek. Only the corresponding provider environment variables
 * (OPENAI_API_KEY / DEEPSEEK_API_KEY) are required at runtime.
 */
export const DEFAULT_CONFIG = Object.freeze({
  schema_version: 1,
  profiles: {
    orchestration: { model: "openai/gpt-5.6-sol" },
    reasoning: { model: "deepseek/deepseek-v4-pro", variant: "high" },
    implementation: { model: "deepseek/deepseek-v4-pro", variant: "max" },
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
    "review-plan-drift": "review",
    "review-quality": "review",
    "review-spec-compliance": "review",
    "review-blind-spots": "review",
    researcher: "extraction",
    "plan-reviewer": "review",
    "code-implementer": "implementation",
    "quality-scanner": "review",
    "spec-reviewer": "review",
    "spec-compliance": "review",
    "drift-detector": "review",
    "blind-spot-finder": "review",
  },
  default_agent: "orchestrator",
})

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function rejectUnknown(value, allowed, context) {
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key))
  if (unknown.length) {
    throw new Error(`${context} has unknown field(s): ${unknown.join(", ")}`)
  }
}

const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9.-]*$/
const AGENT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

/**
 * Parse a `"provider/model-id"` string into `{ providerID, id }`.
 */
export function parseModelRef(model) {
  if (typeof model !== "string" || model.length === 0) {
    throw new Error("profile.model must be a non-empty string")
  }
  const separator = model.indexOf("/")
  if (separator <= 0 || separator === model.length - 1 || model.indexOf("/", separator + 1) !== -1) {
    throw new Error(`profile.model must use provider/model format: ${JSON.stringify(model)}`)
  }
  return {
    providerID: model.slice(0, separator),
    id: model.slice(separator + 1),
  }
}

/**
 * Build a native `ModelRef` ({ providerID, id, variant? }) from a profile.
 */
export function modelRefFor(profile) {
  const { providerID, id } = parseModelRef(profile.model)
  const ref = { providerID, id }
  if (typeof profile.variant === "string" && profile.variant.length > 0) {
    ref.variant = profile.variant
  }
  return ref
}

function validateProfile(profile, context) {
  if (!isObject(profile)) throw new Error(`${context} must be an object`)
  rejectUnknown(profile, ["model", "variant", "request"], context)
  parseModelRef(profile.model)
  if (profile.variant !== undefined && (typeof profile.variant !== "string" || profile.variant.length === 0)) {
    throw new Error(`${context}.variant must be a non-empty string`)
  }
  if (profile.request !== undefined) {
    if (!isObject(profile.request)) throw new Error(`${context}.request must be an object`)
    rejectUnknown(profile.request, ["headers", "body"], `${context}.request`)
    if (profile.request.headers !== undefined && !isObject(profile.request.headers)) {
      throw new Error(`${context}.request.headers must be an object`)
    }
    if (profile.request.body !== undefined && !isObject(profile.request.body)) {
      throw new Error(`${context}.request.body must be an object`)
    }
  }
  return profile
}

/**
 * Validate a full, merged model-router config. Throws on any structural
 * problem.
 *
 * Accepted shape:
 *   {
 *     schema_version: 1,
 *     profiles: { [name]: { model: "provider/model", variant?, request? } },
 *     agents: { [agent-id]: "<profile-name>" },
 *     default_agent: "<agent-id>"   // optional; must resolve to a profile
 *   }
 *
 * - `profiles` is required and non-empty; every profile must be valid.
 * - `agents` maps arbitrary valid agent ids to defined profiles. There is no
 *   closed role list: any id matching a valid identifier and resolving to a
 *   defined profile is accepted.
 * - `default_agent` is optional; when present it must name an agent that maps
 *   to a defined profile.
 */
export function validateConfig(value) {
  if (!isObject(value)) throw new Error("model-router config must be an object")
  rejectUnknown(value, ["schema_version", "profiles", "agents", "default_agent"], "model-router config")

  if (value.schema_version !== SCHEMA_VERSION) {
    throw new Error(`unsupported schema_version: ${String(value.schema_version)}`)
  }
  if (!isObject(value.profiles) || Object.keys(value.profiles).length === 0) {
    throw new Error("profiles must be a non-empty object")
  }
  for (const [name, profile] of Object.entries(value.profiles)) {
    if (!PROFILE_NAME_RE.test(name)) {
      throw new Error(`invalid profile name: ${JSON.stringify(name)}`)
    }
    validateProfile(profile, `profiles.${name}`)
  }

  if (!isObject(value.agents)) throw new Error("agents must be an object")
  for (const [agent, profileName] of Object.entries(value.agents)) {
    if (!AGENT_ID_RE.test(agent)) {
      throw new Error(`invalid agent id: ${JSON.stringify(agent)}`)
    }
    if (typeof profileName !== "string" || profileName.length === 0) {
      throw new Error(`agents.${agent} must name a profile`)
    }
    if (!Object.hasOwn(value.profiles, profileName)) {
      throw new Error(`agents.${agent} references missing profile ${JSON.stringify(profileName)}`)
    }
  }

  if (value.default_agent !== undefined) {
    if (typeof value.default_agent !== "string" || value.default_agent.length === 0) {
      throw new Error("default_agent must name an agent")
    }
    const defaultProfile = value.agents[value.default_agent]
    if (typeof defaultProfile !== "string" || defaultProfile.length === 0) {
      throw new Error(`default_agent ${JSON.stringify(value.default_agent)} has no agent mapping`)
    }
    if (!Object.hasOwn(value.profiles, defaultProfile)) {
      throw new Error(`default_agent ${JSON.stringify(value.default_agent)} references missing profile ${JSON.stringify(defaultProfile)}`)
    }
  }

  return value
}

/**
 * Validate a partial `model_router` override block (the top-level
 * `model_router` of a sandbox config). This is shape-only: cross-references
 * (an agent naming a profile, or `default_agent` resolving) are checked after
 * the override is merged over the defaults, because an override may reference
 * a profile that only the defaults define.
 */
export function validateOverride(value) {
  if (!isObject(value)) throw new Error("model_router must be an object")
  rejectUnknown(value, ["schema_version", "profiles", "agents", "default_agent"], "model_router")

  if (value.schema_version !== undefined && value.schema_version !== SCHEMA_VERSION) {
    throw new Error(`unsupported schema_version: ${String(value.schema_version)}`)
  }
  if (value.profiles !== undefined) {
    if (!isObject(value.profiles)) throw new Error("profiles must be an object")
    for (const [name, profile] of Object.entries(value.profiles)) {
      if (!PROFILE_NAME_RE.test(name)) {
        throw new Error(`invalid profile name: ${JSON.stringify(name)}`)
      }
      validateProfile(profile, `profiles.${name}`)
    }
  }
  if (value.agents !== undefined) {
    if (!isObject(value.agents)) throw new Error("agents must be an object")
    for (const [agent, profileName] of Object.entries(value.agents)) {
      if (!AGENT_ID_RE.test(agent)) {
        throw new Error(`invalid agent id: ${JSON.stringify(agent)}`)
      }
      if (typeof profileName !== "string" || profileName.length === 0) {
        throw new Error(`agents.${agent} must name a profile`)
      }
    }
  }
  if (value.default_agent !== undefined) {
    if (typeof value.default_agent !== "string" || value.default_agent.length === 0) {
      throw new Error("default_agent must name an agent")
    }
  }

  return value
}

/**
 * Shallow-merge a partial override over a full base config: profile and agent
 * maps merge at the top-level key (an override entry replaces the base entry
 * for that name; profile objects are not deep-merged), and
 * `default_agent`/`schema_version` fall back to the base when absent. The
 * override is shape-validated first; the caller validates the merged result.
 */
export function mergeConfig(base, override) {
  validateOverride(override)
  const merged = {
    schema_version: override.schema_version ?? base.schema_version,
    profiles: { ...base.profiles, ...(override.profiles ?? {}) },
    agents: { ...base.agents, ...(override.agents ?? {}) },
  }
  const defaultAgent = override.default_agent ?? base.default_agent
  if (defaultAgent !== undefined) merged.default_agent = defaultAgent
  return merged
}

/**
 * Read a launcher-mounted sandbox JSON and return its top-level `model_router`
 * block, or `undefined` when the file has no such key. Throws on an unreadable
 * path, invalid JSON, or a non-object document.
 */
export async function readModelRouterOverride(path) {
  let text
  try {
    text = await readFile(path, "utf8")
  } catch (err) {
    throw new Error(`unable to read model-router config ${JSON.stringify(path)}: ${err.message}`)
  }

  let sandbox
  try {
    sandbox = JSON.parse(text)
  } catch (err) {
    throw new Error(`invalid JSON in model-router config ${JSON.stringify(path)}: ${err.message}`)
  }
  if (!isObject(sandbox)) {
    throw new Error(`model-router config ${JSON.stringify(path)} must be a JSON object`)
  }
  return sandbox.model_router
}

/**
 * Apply a validated config to an `AgentDraft`:
 *   - for every listed agent whose id has an agent mapping, set
 *     `agent.model` to the profile's `ModelRef` and merge any optional
 *     request headers/body into `agent.request`;
 *   - call `draft.default(config.default_agent)` when a default is set.
 *
 * Pure: operates only on the supplied draft; does not touch the filesystem,
 * the SDK client, or any v1 hook surface.
 */
export function applyAgentConfig(draft, config) {
  const resolved = validateConfig(config)
  for (const agent of draft.list()) {
    const profileName = resolved.agents[agent.id]
    if (typeof profileName !== "string" || profileName.length === 0) continue
    const profile = resolved.profiles[profileName]
    if (!profile) continue
    draft.update(agent.id, (item) => {
      item.model = modelRefFor(profile)
      if (isObject(profile.request)) {
        if (isObject(profile.request.headers)) {
          item.request.headers = { ...(item.request.headers ?? {}), ...profile.request.headers }
        }
        if (isObject(profile.request.body)) {
          item.request.body = { ...(item.request.body ?? {}), ...profile.request.body }
        }
      }
    })
  }
  if (typeof resolved.default_agent === "string" && resolved.default_agent.length > 0) {
    draft.default(resolved.default_agent)
  }
  return resolved
}

export const plugin = define({
  id: "opencode-model-router",
  setup: async (ctx) => {
    const options = ctx.options
    const base = isObject(options) && Object.keys(options).length > 0 ? validateConfig(options) : DEFAULT_CONFIG

    let config = base
    const overridePath = process.env.OPENCODE_MODEL_ROUTER_CONFIG
    if (typeof overridePath === "string" && overridePath.length > 0) {
      const override = await readModelRouterOverride(overridePath)
      if (override !== undefined) {
        config = validateConfig(mergeConfig(base, override))
      }
    }

    await ctx.agent.transform((draft) => {
      applyAgentConfig(draft, config)
    })
  },
})

export default plugin
