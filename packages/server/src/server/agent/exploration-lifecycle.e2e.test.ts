import os from "node:os";
import path from "node:path";
import { execSync } from "node:child_process";
import { access, mkdtemp, rm, writeFile } from "node:fs/promises";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { experimental_createMCPClient } from "ai";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { z } from "zod";
import pino from "pino";

import { PARENT_AGENT_ID_LABEL } from "@getpaseo/protocol/agent-labels";
import { createTestPaseoDaemon, type TestPaseoDaemon } from "../test-utils/paseo-daemon.js";
import { createTestAgentClients } from "../test-utils/fake-agent-client.js";
import type { AgentClient, AgentPromptInput, AgentSessionConfig } from "./agent-sdk-types.js";
import { FileBackedWorkspaceRegistry } from "../workspace-registry.js";

const CODEX_PROVIDER = "codex/gpt-5.4-mini";
const ORDINARY_SETTINGS = {
  modeId: "full-access",
  thinkingOptionId: "high",
  features: { plan_mode: false },
};
const PLAN_SETTINGS = {
  modeId: "full-access",
  thinkingOptionId: "high",
  features: { plan_mode: true },
};
const EXPECTED_FINDINGS = "ordinary-child-findings-ok";
const MARKER_FILE = "keep-this-file.txt";

interface StructuredContent {
  [key: string]: unknown;
}

interface McpToolResult {
  structuredContent?: StructuredContent;
  content?: Array<{ structuredContent?: StructuredContent } | StructuredContent>;
  isError?: boolean;
}

interface McpClient {
  callTool: (input: { name: string; args?: StructuredContent }) => Promise<McpToolResult>;
  close: () => Promise<void>;
}

const launchConfigs: AgentSessionConfig[] = [];
const startTurns: string[] = [];

function str(val: unknown): string {
  return z.string().parse(val);
}

function promptText(prompt: AgentPromptInput): string {
  return typeof prompt === "string" ? prompt : JSON.stringify(prompt);
}

function getStructuredContent(result: McpToolResult): StructuredContent | null {
  if (result.structuredContent && typeof result.structuredContent === "object") {
    return result.structuredContent;
  }
  const content = result.content?.[0];
  if (content && typeof content === "object" && "structuredContent" in content) {
    if (content.structuredContent) {
      return content.structuredContent;
    }
  }
  if (content && typeof content === "object") {
    return content;
  }
  return null;
}

async function createMcpClient(url: string): Promise<McpClient> {
  const transport = new StreamableHTTPClientTransport(new URL(url));
  const rawClient = await experimental_createMCPClient({ transport });
  const boundCallTool: McpClient["callTool"] = Reflect.get(rawClient, "callTool").bind(rawClient);
  return { callTool: boundCallTool, close: () => rawClient.close() };
}

function toolErrorText(result: McpToolResult): string {
  const contentItem = result.content?.[0];
  if (contentItem != null && typeof contentItem === "object") {
    const text = Reflect.get(contentItem, "text");
    if (typeof text === "string" && text.length > 0) {
      return text;
    }
  }
  return JSON.stringify(result);
}

async function callToolStructured(
  client: McpClient,
  name: string,
  args?: StructuredContent,
): Promise<StructuredContent> {
  const result = await client.callTool({ name, args: args ?? {} });
  if (result.isError) {
    throw new Error(`${name} failed: ${toolErrorText(result)}`);
  }
  const payload = getStructuredContent(result);
  if (!payload) {
    throw new Error(`${name} returned no structured payload`);
  }
  return payload;
}

async function expectToolError(
  client: McpClient,
  name: string,
  args: StructuredContent,
  pattern: RegExp,
): Promise<void> {
  const result = await client.callTool({ name, args });
  expect(result.isError).toBe(true);
  const contentItem = result.content?.[0];
  const contentText: string | undefined =
    contentItem != null && typeof contentItem === "object"
      ? Reflect.get(contentItem, "text")
      : undefined;
  expect(contentText ?? "").toMatch(pattern);
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor<T>(options: {
  timeoutMs: number;
  intervalMs?: number;
  check: () => Promise<T | null> | T | null;
  label: string;
}): Promise<T> {
  const start = Date.now();
  while (Date.now() - start < options.timeoutMs) {
    const result = await options.check();
    if (result !== null) {
      return result;
    }
    await sleep(options.intervalMs ?? 50);
  }
  throw new Error(`Timed out after ${options.timeoutMs}ms waiting for ${options.label}`);
}

function createRecordingAgentClients(): Record<string, AgentClient> {
  launchConfigs.length = 0;
  startTurns.length = 0;
  const baseClients = createTestAgentClients({
    onStartTurn: (prompt) => {
      startTurns.push(promptText(prompt));
    },
  });
  const wrappedClients: Record<string, AgentClient> = {};
  for (const [provider, client] of Object.entries(baseClients)) {
    const wrappedClient: AgentClient = {
      provider: client.provider,
      capabilities: client.capabilities,
      createSession: async (config, launchContext, options) => {
        launchConfigs.push(config);
        return await client.createSession(config, launchContext, options);
      },
      resumeSession: async (handle, overrides, launchContext) =>
        await client.resumeSession(handle, overrides, launchContext),
      fetchCatalog: async (options) => await client.fetchCatalog(options),
      isAvailable: async () => await client.isAvailable(),
    };
    wrappedClients[provider] = wrappedClient;
  }
  return wrappedClients;
}

async function readWorkspaces(paseoHome: string) {
  const registry = new FileBackedWorkspaceRegistry(
    path.join(paseoHome, "projects", "workspaces.json"),
    pino({ level: "silent" }),
  );
  await registry.initialize();
  const all = await registry.list();
  return {
    all,
    active: all.filter((workspace) => !workspace.archivedAt),
  };
}

function launchConfigByTitle(title: string): AgentSessionConfig {
  const config = launchConfigs.find((entry) => entry.title === title);
  if (!config) {
    throw new Error(`No captured createSession config titled ${title}`);
  }
  return config;
}

let tempRoot: string;
let daemonHandle: TestPaseoDaemon;
let topLevelClient: McpClient;
let agentScopedClient: McpClient;
let parentAgentId: string;
let parentWorkspaceId: string;
let parentCwd: string;

beforeAll(async () => {
  tempRoot = await mkdtemp(path.join(os.tmpdir(), "exploration-lifecycle-e2e-"));
  parentCwd = await mkdtemp(path.join(tempRoot, "parent-cwd-"));
  execSync("git init -b main", { cwd: parentCwd, stdio: "pipe" });
  execSync("git config user.email 'test@example.com'", { cwd: parentCwd, stdio: "pipe" });
  execSync("git config user.name 'Test User'", { cwd: parentCwd, stdio: "pipe" });
  await writeFile(path.join(parentCwd, MARKER_FILE), "preserve me\n", "utf8");
  execSync(`git add ${MARKER_FILE}`, { cwd: parentCwd, stdio: "pipe" });
  execSync("git -c commit.gpgsign=false commit -m init", { cwd: parentCwd, stdio: "pipe" });

  daemonHandle = await createTestPaseoDaemon({ agentClients: createRecordingAgentClients() });
  topLevelClient = await createMcpClient(`http://127.0.0.1:${daemonHandle.port}/mcp/agents`);

  const parentPayload = await callToolStructured(topLevelClient, "create_agent", {
    relationship: { kind: "detached" },
    workspace: { kind: "create", source: { kind: "directory", path: parentCwd } },
    title: "Plan parent",
    provider: CODEX_PROVIDER,
    initialPrompt: "say done and stop",
    settings: PLAN_SETTINGS,
    background: false,
  });
  parentAgentId = str(parentPayload.agentId);
  const parentRecord =
    daemonHandle.daemon.agentManager.getAgent(parentAgentId) ??
    (await daemonHandle.daemon.agentStorage.get(parentAgentId));
  if (!parentRecord?.workspaceId) {
    throw new Error("parent directory create did not stamp a workspaceId");
  }
  parentWorkspaceId = parentRecord.workspaceId;

  await waitFor({
    timeoutMs: 10_000,
    label: "parent idle",
    check: () => {
      const parent = daemonHandle.daemon.agentManager.getAgent(parentAgentId);
      return parent?.lifecycle === "idle" ? parent : null;
    },
  });

  agentScopedClient = await createMcpClient(
    `http://127.0.0.1:${daemonHandle.port}/mcp/agents?callerAgentId=${encodeURIComponent(parentAgentId)}`,
  );
}, 30_000);

afterAll(async () => {
  await agentScopedClient?.close();
  await topLevelClient?.close();
  await daemonHandle?.close();
  await rm(tempRoot, { recursive: true, force: true });
});

describe("exploration lifecycle MCP path", () => {
  test("Plan parent launches ordinary current child and existing sibling, then archive keeps workspace", async () => {
    const before = await readWorkspaces(daemonHandle.paseoHome);
    expect(before.active).toHaveLength(1);
    expect(before.active[0]?.workspaceId).toBe(parentWorkspaceId);

    const childPayload = await callToolStructured(agentScopedClient, "create_agent", {
      relationship: { kind: "subagent" },
      workspace: { kind: "current" },
      title: "Ordinary child",
      provider: CODEX_PROVIDER,
      initialPrompt: `Respond with exactly: ${EXPECTED_FINDINGS}`,
      settings: ORDINARY_SETTINGS,
      labels: { lifecycle: "temporary-child", cleanup: "archive-on-finish" },
      notifyOnFinish: true,
    });
    const childId = str(childPayload.agentId);

    const siblingPayload = await callToolStructured(agentScopedClient, "create_agent", {
      relationship: { kind: "detached" },
      workspace: { kind: "existing", workspaceId: parentWorkspaceId },
      title: "Existing sibling",
      provider: CODEX_PROVIDER,
      initialPrompt: "say done and stop",
      settings: ORDINARY_SETTINGS,
      notifyOnFinish: false,
    });
    const siblingId = str(siblingPayload.agentId);

    const afterChildren = await readWorkspaces(daemonHandle.paseoHome);
    expect(afterChildren.all).toHaveLength(before.all.length);
    expect(afterChildren.active.map((workspace) => workspace.workspaceId)).toEqual([
      parentWorkspaceId,
    ]);

    const parent = daemonHandle.daemon.agentManager.getAgent(parentAgentId);
    const child = daemonHandle.daemon.agentManager.getAgent(childId);
    const sibling = daemonHandle.daemon.agentManager.getAgent(siblingId);
    expect(parent?.config.modeId).toBe("full-access");
    expect(parent?.config.thinkingOptionId).toBe("high");
    expect(parent?.config.featureValues).toMatchObject({ plan_mode: true });
    expect(child?.config.modeId).toBe("full-access");
    expect(child?.config.thinkingOptionId).toBe("high");
    expect(child?.config.featureValues).toMatchObject({ plan_mode: false });
    expect(child?.labels).toMatchObject({
      [PARENT_AGENT_ID_LABEL]: parentAgentId,
      lifecycle: "temporary-child",
      cleanup: "archive-on-finish",
    });
    expect(sibling?.labels?.[PARENT_AGENT_ID_LABEL]).toBeUndefined();
    expect(sibling?.workspaceId).toBe(parentWorkspaceId);
    expect(child?.workspaceId).toBe(parentWorkspaceId);

    const parentLaunch = launchConfigByTitle("Plan parent");
    const childLaunch = launchConfigByTitle("Ordinary child");
    expect(parentLaunch.featureValues).toMatchObject({ plan_mode: true });
    expect(parentLaunch.modeId).toBe("full-access");
    expect(parentLaunch.thinkingOptionId).toBe("high");
    expect(childLaunch.featureValues).toMatchObject({ plan_mode: false });
    expect(childLaunch.modeId).toBe("full-access");
    expect(childLaunch.thinkingOptionId).toBe("high");

    await waitFor({
      timeoutMs: 10_000,
      label: "child idle",
      check: () => {
        const live = daemonHandle.daemon.agentManager.getAgent(childId);
        return live?.lifecycle === "idle" ? live : null;
      },
    });
    expect(await daemonHandle.daemon.agentManager.getLastAssistantMessage(childId)).toBe(
      EXPECTED_FINDINGS,
    );

    const notification = await waitFor({
      timeoutMs: 10_000,
      label: "parent finish notification",
      check: () =>
        startTurns.find(
          (prompt) => prompt.includes(childId) && prompt.includes("<agent-response>"),
        ) ?? null,
    });
    expect(notification).toContain(EXPECTED_FINDINGS);
    expect(notification).toContain(childId);

    await callToolStructured(agentScopedClient, "archive_agent", { agentId: childId });

    expect(daemonHandle.daemon.agentManager.getAgent(childId)).toBeNull();
    expect(daemonHandle.daemon.agentManager.getAgent(parentAgentId)?.lifecycle).toBe("idle");
    expect(daemonHandle.daemon.agentManager.getAgent(siblingId)).not.toBeNull();
    const storedChild = await daemonHandle.daemon.agentStorage.get(childId);
    const storedParent = await daemonHandle.daemon.agentStorage.get(parentAgentId);
    const storedSibling = await daemonHandle.daemon.agentStorage.get(siblingId);
    expect(storedChild?.archivedAt).toEqual(expect.any(String));
    expect(storedParent?.archivedAt ?? null).toBeNull();
    expect(storedSibling?.archivedAt ?? null).toBeNull();

    const afterArchive = await readWorkspaces(daemonHandle.paseoHome);
    expect(afterArchive.active.map((workspace) => workspace.workspaceId)).toEqual([
      parentWorkspaceId,
    ]);
    expect(afterArchive.all).toHaveLength(before.all.length);
  });

  test("invalid existing workspace id allocates neither a workspace nor an agent", async () => {
    const beforeWorkspaces = await readWorkspaces(daemonHandle.paseoHome);
    const beforeAgents = daemonHandle.daemon.agentManager.listAgents().map((agent) => agent.id);
    const beforeStored = (await daemonHandle.daemon.agentStorage.list()).map((record) => record.id);

    await expectToolError(
      agentScopedClient,
      "create_agent",
      {
        relationship: { kind: "subagent" },
        workspace: { kind: "existing", workspaceId: "wks_missing" },
        title: "Missing workspace",
        provider: CODEX_PROVIDER,
        initialPrompt: "should not launch",
        settings: ORDINARY_SETTINGS,
      },
      /Unknown workspace: wks_missing/,
    );

    const afterWorkspaces = await readWorkspaces(daemonHandle.paseoHome);
    expect(afterWorkspaces.all).toHaveLength(beforeWorkspaces.all.length);
    expect(afterWorkspaces.active).toHaveLength(beforeWorkspaces.active.length);
    expect(daemonHandle.daemon.agentManager.listAgents().map((agent) => agent.id)).toEqual(
      beforeAgents,
    );
    expect((await daemonHandle.daemon.agentStorage.list()).map((record) => record.id)).toEqual(
      beforeStored,
    );
  });

  test("invalid directory create leaves no new active workspace and preserves the marker file", async () => {
    const before = await readWorkspaces(daemonHandle.paseoHome);
    const beforeAgents = daemonHandle.daemon.agentManager.listAgents().map((agent) => agent.id);

    await expectToolError(
      agentScopedClient,
      "create_agent",
      {
        relationship: { kind: "detached" },
        workspace: { kind: "create", source: { kind: "directory", path: parentCwd } },
        title: "Invalid directory",
        provider: CODEX_PROVIDER,
        initialPrompt: "should not launch",
        settings: { ...ORDINARY_SETTINGS, modeId: "nope" },
      },
      /Invalid mode 'nope' for provider 'codex'/,
    );

    await access(path.join(parentCwd, MARKER_FILE));
    const after = await readWorkspaces(daemonHandle.paseoHome);
    expect(after.active.map((workspace) => workspace.workspaceId)).toEqual(
      before.active.map((workspace) => workspace.workspaceId),
    );
    expect(after.active.some((workspace) => workspace.workspaceId === parentWorkspaceId)).toBe(
      true,
    );
    expect(daemonHandle.daemon.agentManager.listAgents().map((agent) => agent.id)).toEqual(
      beforeAgents,
    );
  });
});
