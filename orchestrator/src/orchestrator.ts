#!/usr/bin/env node
/**
 * Cursor cloud-agent orchestrator.
 *
 * Reads a JSON request from stdin, loads the named orchestrator config,
 * spawns a parent cloud agent with named sub-agents, and streams NDJSON trace
 * events to stdout for the Python backend to consume.
 *
 * stdin (JSON):
 *   {
 *     "config":       "care-plan",
 *     "flow":         "weekly_plan" | "symptom_adjustment" | "intake" | "checkin",
 *     "repoUrl":      "https://github.com/AndreChuabio/rehab-protocols-andre",
 *     "extraPrompt":  "optional natural-language task addendum",
 *     "contextFiles": { "data/wearables.json": "...", ... }
 *   }
 *
 * stdout (NDJSON, one JSON object per line):
 *   { "type": "trace",  "event": { ...TraceEvent } }
 *   ...
 *   { "type": "result", "agentId": "bc-...", "runId": "...",
 *     "prUrl": "...", "branch": "...", "status": "finished" }
 *
 * stderr: human-readable diagnostics.
 *
 * exit codes:
 *   0 = finished successfully
 *   1 = startup / config / auth failure
 *   2 = run started but ended in error
 */

import {
  Agent,
  CursorAgentError,
  type AgentDefinition,
  type ModelSelection,
  type SDKAgent,
  type SDKMessage,
  type RunResult,
} from "@cursor/sdk";
import { loadConfig, type OrchestratorConfig } from "./config.ts";

interface Request {
  config: string;
  flow: string;
  repoUrl: string;
  extraPrompt?: string;
  contextFiles?: Record<string, string>;
}

interface TraceEvent {
  type: string;
  timestamp: number;
  label: string;
  payload?: Record<string, unknown>;
}

function emitTrace(event: TraceEvent): void {
  process.stdout.write(JSON.stringify({ type: "trace", event }) + "\n");
}

function emitResult(result: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify({ type: "result", ...result }) + "\n");
}

async function readStdin(): Promise<string> {
  return await new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function buildPrompt(
  cfg: OrchestratorConfig,
  flow: string,
  extra: string,
  contextFiles: Record<string, string> | undefined
): string {
  const flowCfg = cfg.flows[flow];
  if (!flowCfg) {
    const available = Object.keys(cfg.flows).join(", ");
    throw new Error(`unknown flow "${flow}". available: ${available}`);
  }
  const extraBlock = extra ? `\n\nADDITIONAL CONTEXT:\n${extra}` : "";
  const ctxBlock = contextFiles && Object.keys(contextFiles).length > 0
    ? "\n\nCONTEXT FILES (inlined; the agent should treat these as if they had just been committed to the repo):\n" +
      Object.entries(contextFiles)
        .map(([path, content]) => `--- ${path} ---\n${content}`)
        .join("\n\n")
    : "";
  return `${cfg.parent.prompt}\n\n${flowCfg.addon}${extraBlock}${ctxBlock}`;
}

function buildSubagents(cfg: OrchestratorConfig): Record<string, AgentDefinition> {
  const out: Record<string, AgentDefinition> = {};
  for (const [key, sub] of Object.entries(cfg.subagents)) {
    const modelSel: AgentDefinition["model"] =
      !sub.model || sub.model === "inherit"
        ? "inherit"
        : ({ id: sub.model } as ModelSelection);
    out[key] = {
      description: sub.description,
      prompt: sub.prompt,
      model: modelSel,
      ...(sub.mcpServers ? { mcpServers: sub.mcpServers } : {}),
    };
  }
  return out;
}

async function main(): Promise<number> {
  const apiKey = process.env.CURSOR_API_KEY;
  if (!apiKey) {
    console.error("CURSOR_API_KEY not set");
    emitTrace({
      type: "agent_failed",
      timestamp: 0,
      label: "CURSOR_API_KEY not set",
    });
    return 1;
  }

  const rawStdin = await readStdin();
  let req: Request;
  try {
    req = JSON.parse(rawStdin) as Request;
  } catch (err) {
    console.error("invalid JSON on stdin:", err);
    return 1;
  }

  const cfg = loadConfig(req.config);
  const flowCfg = cfg.flows[req.flow];
  if (!flowCfg) {
    console.error(`unknown flow "${req.flow}"`);
    return 1;
  }

  const prompt = buildPrompt(cfg, req.flow, req.extraPrompt ?? "", req.contextFiles);
  const start = Date.now();
  const t = (): number => (Date.now() - start) / 1000;

  emitTrace({
    type: "agent_started",
    timestamp: t(),
    label: `orchestrator "${cfg.name}" starting flow "${req.flow}"`,
    payload: {
      config: req.config,
      flow: req.flow,
      subagents: Object.keys(cfg.subagents),
    },
  });

  let agent: SDKAgent | undefined;
  try {
    const startingRef = process.env.PROTOCOL_BRANCH || "main";
    agent = await Agent.create({
      apiKey,
      model: { id: cfg.parent.model },
      cloud: {
        repos: [{ url: req.repoUrl, startingRef }],
        autoCreatePR: flowCfg.autoCreatePR,
        skipReviewerRequest: flowCfg.skipReviewerRequest,
      },
      agents: buildSubagents(cfg),
    });

    const run = await agent.send(prompt);
    console.error(`agentId=${agent.agentId} runId=${run.id}`);
    emitTrace({
      type: "tool_call",
      timestamp: t(),
      label: `run started (agentId=${agent.agentId}, runId=${run.id})`,
      payload: { agentId: agent.agentId, runId: run.id },
    });

    for await (const event of run.stream()) {
      const mapped = mapSdkMessage(event, t());
      if (mapped) emitTrace(mapped);
    }

    const result = await run.wait();

    if (result.status === "error") {
      emitTrace({
        type: "agent_failed",
        timestamp: t(),
        label: "run ended in error",
        payload: { runId: run.id },
      });
      emitResult({
        agentId: agent.agentId,
        runId: run.id,
        status: result.status,
      });
      return 2;
    }

    const prUrl = extractPrUrl(result);
    const branch = extractBranch(result);
    emitTrace({
      type: "pr_opened",
      timestamp: t(),
      label: prUrl ? `PR opened: ${prUrl}` : "run complete (no PR)",
      payload: { prUrl, branch },
    });
    emitTrace({
      type: "agent_completed",
      timestamp: t(),
      label: "orchestrator finished",
    });
    emitResult({
      agentId: agent.agentId,
      runId: run.id,
      prUrl,
      branch,
      status: result.status,
    });
    return 0;
  } catch (err) {
    if (err instanceof CursorAgentError) {
      console.error(
        `startup failure: ${err.message} retryable=${err.isRetryable}`
      );
      emitTrace({
        type: "agent_failed",
        timestamp: t(),
        label: `startup failure: ${err.message}`,
        payload: { retryable: err.isRetryable },
      });
      return 1;
    }
    const msg = err instanceof Error ? err.message : String(err);
    console.error("unexpected error:", err);
    emitTrace({
      type: "agent_failed",
      timestamp: t(),
      label: `unexpected error: ${msg}`,
    });
    return 1;
  } finally {
    if (agent) {
      try {
        await agent[Symbol.asyncDispose]();
      } catch {
        // disposal errors are non-fatal for the demo path
      }
    }
  }
}

/**
 * Best-effort mapping from SDK stream messages to our stable TraceEvent shape.
 *
 * The SDK emits SDKMessage union types:
 *   system | user | assistant | tool_call | thinking | status | request | task
 *
 * We surface the ones users want to see in the trace panel. Anything we don't
 * classify is dropped rather than polluting the UI. The Python bridge renders
 * whatever we emit.
 */
function mapSdkMessage(msg: SDKMessage, ts: number): TraceEvent | null {
  switch (msg.type) {
    case "system":
    case "user":
    case "assistant":
    case "thinking":
    case "request":
      return null;

    case "status": {
      // Surface state transitions: CREATING -> RUNNING -> FINISHED etc.
      const isTerminal =
        msg.status === "FINISHED" ||
        msg.status === "ERROR" ||
        msg.status === "CANCELLED" ||
        msg.status === "EXPIRED";
      if (isTerminal) return null; // terminal state handled by run.wait()
      return {
        type: "tool_call",
        timestamp: ts,
        label: `status: ${msg.status}${msg.message ? ` — ${msg.message}` : ""}`,
        payload: { status: msg.status },
      };
    }

    case "task": {
      return {
        type: "tool_call",
        timestamp: ts,
        label: `task: ${msg.text ?? msg.status ?? "(unlabeled)"}`,
        payload: { status: msg.status, text: msg.text },
      };
    }

    case "tool_call": {
      if (msg.status !== "running") return null;
      const name = msg.name;
      const args = (msg.args as Record<string, unknown> | undefined) ?? {};

      // The Cursor "task" tool is how a parent spawns a named sub-agent.
      // args typically contains something like { subagent_type, description, prompt }.
      if (name === "task" || name === "Agent") {
        const sub =
          (args.subagent_type as string | undefined) ??
          (args.agent as string | undefined) ??
          (args.name as string | undefined);
        if (sub) {
          return {
            type: "tool_call",
            timestamp: ts,
            label: `spawn sub-agent: ${sub}`,
            payload: { subagent: sub, tool: name, args },
          };
        }
      }

      const path =
        (args.path as string | undefined) ??
        (args.file as string | undefined) ??
        (args.target_file as string | undefined);

      if (typeof path === "string") {
        if (
          name === "edit_file" ||
          name === "edit" ||
          name === "write" ||
          name === "apply_patch"
        ) {
          return {
            type: "file_edit",
            timestamp: ts,
            label: `edit ${path}`,
            payload: { path, tool: name },
          };
        }
        if (name === "read" || name === "read_file") {
          return {
            type: "file_read",
            timestamp: ts,
            label: `read ${path}`,
            payload: { path, tool: name },
          };
        }
      }

      if (name === "shell") {
        const cmd = (args.command as string | undefined) ?? "";
        if (/git checkout -b /.test(cmd)) {
          const ref = cmd.split("-b").pop()?.trim();
          return {
            type: "branch_created",
            timestamp: ts,
            label: `git checkout -b ${ref}`,
            payload: { ref, command: cmd },
          };
        }
        if (/^git commit /.test(cmd) || /\bgit commit\b/.test(cmd)) {
          return {
            type: "commit_created",
            timestamp: ts,
            label: cmd.slice(0, 80),
            payload: { command: cmd },
          };
        }
      }

      return {
        type: "tool_call",
        timestamp: ts,
        label: name,
        payload: { tool: name, args },
      };
    }
  }
}

function extractPrUrl(result: RunResult): string | undefined {
  const branches = result.git?.branches ?? [];
  for (const b of branches) {
    if (b.prUrl) return b.prUrl;
  }
  return undefined;
}

function extractBranch(result: RunResult): string | undefined {
  const branches = result.git?.branches ?? [];
  for (const b of branches) {
    if (b.branch) return b.branch;
  }
  return undefined;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error("fatal:", err);
    process.exit(1);
  }
);
