import fs from "node:fs";
import path from "node:path";
import url from "node:url";
import yaml from "js-yaml";

export interface SubagentConfig {
  description: string;
  prompt: string;
  model?: string;
  mcpServers?: string[];
}

export interface FlowConfig {
  addon: string;
  autoCreatePR: boolean;
  skipReviewerRequest: boolean;
}

export interface ScopeConfig {
  reads: string[];
  writes: string[];
}

export interface OrchestratorConfig {
  name: string;
  description: string;
  parent: {
    model: string;
    prompt: string;
  };
  subagents: Record<string, SubagentConfig>;
  scope: ScopeConfig;
  flows: Record<string, FlowConfig>;
}

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
const CONFIG_DIR = path.resolve(HERE, "..", "configs");

/**
 * Load an orchestrator config by name.
 *
 * New orchestrators drop in as {name}.yaml in orchestrator/configs/. No code
 * changes required. The name is the filename without the .yaml extension.
 */
export function loadConfig(name: string): OrchestratorConfig {
  const file = path.join(CONFIG_DIR, `${name}.yaml`);
  if (!fs.existsSync(file)) {
    const available = fs
      .readdirSync(CONFIG_DIR)
      .filter((f) => f.endsWith(".yaml"))
      .map((f) => f.replace(/\.yaml$/, ""));
    throw new Error(
      `orchestrator config "${name}" not found at ${file}. ` +
        `Available: ${available.join(", ")}`
    );
  }
  const raw = fs.readFileSync(file, "utf8");
  const parsed = yaml.load(raw) as OrchestratorConfig;
  validateConfig(parsed, name);
  return parsed;
}

function validateConfig(cfg: OrchestratorConfig, name: string): void {
  const required: Array<keyof OrchestratorConfig> = [
    "name",
    "parent",
    "subagents",
    "scope",
    "flows",
  ];
  for (const key of required) {
    if (!cfg[key]) {
      throw new Error(
        `orchestrator config ${name} missing required key ${String(key)}`
      );
    }
  }
  if (!cfg.parent.prompt) {
    throw new Error(`orchestrator config ${name}: parent.prompt is required`);
  }
  if (Object.keys(cfg.subagents).length === 0) {
    throw new Error(
      `orchestrator config ${name}: at least one subagent is required`
    );
  }
}
