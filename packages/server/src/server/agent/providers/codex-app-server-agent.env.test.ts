import type { ChildProcessWithoutNullStreams } from "node:child_process";
import fs, { appendFile, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";

import { createTestLogger } from "../../../test-utils/test-logger.js";
import { asInternals as castInternals } from "../../test-utils/class-mocks.js";
import type { AgentLaunchContext } from "../../agent-sdk-types.js";
import { CodexAppServerAgentClient } from "./codex-app-server-agent.js";
import { createFakeCodexAppServer } from "./codex/test-utils/fake-app-server.js";

const ROOT_THREAD_ID = "019fc5d4-2438-7760-a6f6-44704be5b045";
const CHILD_THREAD_ID = "11111111-2222-3333-4444-555555555555";
const ROOT_ROLLOUT_REL = join(
  "sessions",
  "2026",
  "08",
  "03",
  `rollout-2026-08-03T12-14-02-${ROOT_THREAD_ID}.jsonl`,
);
const CHILD_ROLLOUT_REL = join(
  "sessions",
  "2026",
  "08",
  "03",
  `rollout-2026-08-03T12-15-40-${CHILD_THREAD_ID}.jsonl`,
);

interface ClientInternals {
  goalsEnabledPromise: Promise<boolean> | null;
  autoReviewEnabledPromise: Promise<boolean> | null;
  spawnAppServer: () => Promise<ChildProcessWithoutNullStreams>;
}

const createdRoots: string[] = [];

afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(
    createdRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })),
  );
});

async function createHomes(): Promise<{ root: string; sourceHome: string; targetHome: string }> {
  const root = await mkdtemp(join(tmpdir(), "paseo-codex-profile-switch-"));
  createdRoots.push(root);
  return { root, sourceHome: join(root, "source"), targetHome: join(root, "target") };
}

async function writeRollout(home: string, relative: string, content: string): Promise<void> {
  const absolute = join(home, relative);
  await mkdir(dirname(absolute), { recursive: true });
  await writeFile(absolute, content);
}

function rootRolloutContent(extra = ""): string {
  return (
    JSON.stringify({
      timestamp: "2026-08-03T12:14:02.000Z",
      type: "session_meta",
      payload: { id: ROOT_THREAD_ID, cwd: "/workspace/project" },
    }) +
    "\n" +
    JSON.stringify({
      timestamp: "2026-08-03T12:15:40.000Z",
      type: "response_item",
      payload: { type: "agent", agentThreadId: CHILD_THREAD_ID },
    }) +
    extra +
    "\n"
  );
}

function createProfileClient(targetHome: string): CodexAppServerAgentClient {
  const appServer = createFakeCodexAppServer();
  const client = new CodexAppServerAgentClient(createTestLogger(), {
    env: { CODEX_HOME: targetHome },
  });
  const internals = castInternals<ClientInternals>(client);
  internals.goalsEnabledPromise = Promise.resolve(false);
  internals.autoReviewEnabledPromise = Promise.resolve(false);
  internals.spawnAppServer = async () => appServer.child;
  return client;
}

async function resumeIntoTarget(
  client: CodexAppServerAgentClient,
  sourceHome: string,
  launchContext?: AgentLaunchContext,
) {
  return client.resumeSession(
    {
      sessionId: ROOT_THREAD_ID,
      metadata: { cwd: "/workspace/project", paseoCodexHome: sourceHome },
    },
    { cwd: "/workspace/project", modeId: "auto", model: "gpt-5.4" },
    launchContext,
  );
}

describe("Codex profile home switching", () => {
  test("copies a resumed session and its referenced child rollouts into the target home", async () => {
    const { sourceHome, targetHome } = await createHomes();
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, rootRolloutContent());
    await writeRollout(sourceHome, CHILD_ROLLOUT_REL, '{"child":true}\n');

    const client = createProfileClient(targetHome);
    const session = await resumeIntoTarget(client, sourceHome);
    try {
      await expect(readFile(join(targetHome, ROOT_ROLLOUT_REL), "utf8")).resolves.toBe(
        rootRolloutContent(),
      );
      await expect(readFile(join(targetHome, CHILD_ROLLOUT_REL), "utf8")).resolves.toBe(
        '{"child":true}\n',
      );
      expect(session.describePersistence()?.metadata).toMatchObject({
        paseoCodexHome: targetHome,
      });
    } finally {
      await session.close();
    }
  });

  test("resolves the target home from launch-context env before profile settings", async () => {
    const { root, sourceHome, targetHome } = await createHomes();
    const launchHome = join(root, "launch");
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, rootRolloutContent());

    const client = createProfileClient(targetHome);
    const session = await resumeIntoTarget(client, sourceHome, {
      agentId: "agent-1",
      env: { CODEX_HOME: launchHome },
    });
    try {
      await expect(readFile(join(launchHome, ROOT_ROLLOUT_REL), "utf8")).resolves.toBe(
        rootRolloutContent(),
      );
      expect(session.describePersistence()?.metadata).toMatchObject({
        paseoCodexHome: launchHome,
      });
    } finally {
      await session.close();
    }
  });

  test("rejects when the source rollout does not exist", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const client = createProfileClient(targetHome);

    await expect(resumeIntoTarget(client, sourceHome)).rejects.toThrow(
      `Codex session rollout '${ROOT_THREAD_ID}' was not found`,
    );
  });

  test("preserves a conflicting account copy and resumes the selected source conversation", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const common = rootRolloutContent();
    const source =
      common +
      '{"type":"response_item","payload":{"type":"message","content":"continued conversation"}}\n';
    const previous = common + '{"type":"event_msg","payload":{"type":"item_completed"}}\n';
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, source);
    await writeRollout(targetHome, ROOT_ROLLOUT_REL, previous);
    const session = await resumeIntoTarget(createProfileClient(targetHome), sourceHome);
    try {
      expect(await readFile(join(targetHome, ROOT_ROLLOUT_REL), "utf8")).toBe(source);
      expect(await readFile(join(sourceHome, ROOT_ROLLOUT_REL), "utf8")).toBe(source);
      const backupDir = join(targetHome, "paseo-profile-transfer-backups", ROOT_THREAD_ID);
      const backups = await readdir(backupDir);
      expect(backups).toHaveLength(1);
      expect(await readFile(join(backupDir, backups[0]!), "utf8")).toBe(previous);
    } finally {
      await session.close();
    }
  });

  test("retains the target when publication fails and removes the staged file", async () => {
    const { sourceHome, targetHome } = await createHomes();
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, rootRolloutContent());
    const previous = '{"different":"conversation"}\n';
    await writeRollout(targetHome, ROOT_ROLLOUT_REL, previous);
    vi.spyOn(fs, "rename").mockRejectedValueOnce(new Error("publication failed"));
    await expect(resumeIntoTarget(createProfileClient(targetHome), sourceHome)).rejects.toThrow(
      "publication failed",
    );
    expect(await readFile(join(targetHome, ROOT_ROLLOUT_REL), "utf8")).toBe(previous);
    expect(
      (await readdir(dirname(join(targetHome, ROOT_ROLLOUT_REL)))).some((name) =>
        name.startsWith(".paseo-transfer-"),
      ),
    ).toBe(false);
  });

  test("refuses a changing source without publishing a partial snapshot", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const sourcePath = join(sourceHome, ROOT_ROLLOUT_REL);
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, rootRolloutContent());
    const previous = '{"existing":true}\n';
    await writeRollout(targetHome, ROOT_ROLLOUT_REL, previous);
    const copy = fs.copyFile;
    vi.spyOn(fs, "copyFile").mockImplementation(async (...args) => {
      await copy(...args);
      await appendFile(sourcePath, '{"late":true}\n');
    });
    await expect(resumeIntoTarget(createProfileClient(targetHome), sourceHome)).rejects.toThrow(
      "changed during transfer",
    );
    expect(await readFile(join(targetHome, ROOT_ROLLOUT_REL), "utf8")).toBe(previous);
  });

  test("retains a destination changed by another writer while staging", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const targetPath = join(targetHome, ROOT_ROLLOUT_REL);
    const previous = '{"existing":true}\n';
    const late = '{"late":true}\n';
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, rootRolloutContent());
    await writeRollout(targetHome, ROOT_ROLLOUT_REL, previous);
    const copy = fs.copyFile;
    vi.spyOn(fs, "copyFile").mockImplementation(async (...args) => {
      await copy(...args);
      await appendFile(targetPath, late);
    });
    await expect(resumeIntoTarget(createProfileClient(targetHome), sourceHome)).rejects.toThrow(
      "changed in the target profile",
    );
    expect(await readFile(targetPath, "utf8")).toBe(previous + late);
  });

  test("rejects a rollout disappearing after indexing without an unhandled stream error", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const sourcePath = join(sourceHome, ROOT_ROLLOUT_REL);
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, rootRolloutContent());
    const list = fs.readdir;
    vi.spyOn(fs, "readdir").mockImplementation(async (...args) => {
      const entries = await list(...args);
      if (String(args[0]) === dirname(sourcePath)) await rm(sourcePath);
      return entries;
    });
    await expect(
      resumeIntoTarget(createProfileClient(targetHome), sourceHome),
    ).rejects.toMatchObject({ code: "ENOENT" });
  });

  test("does not copy UUIDs quoted in messages or unrelated tool output", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const content =
      [
        { type: "session_meta", payload: { id: ROOT_THREAD_ID } },
        {
          type: "response_item",
          payload: { type: "message", content: `Example agentThreadId: ${CHILD_THREAD_ID}` },
        },
        {
          type: "response_item",
          payload: {
            type: "function_call_output",
            call_id: "unrelated",
            output: JSON.stringify({ agent_id: CHILD_THREAD_ID }),
          },
        },
      ]
        .map((record) => JSON.stringify(record))
        .join("\n") + "\n";
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, content);
    await writeRollout(sourceHome, CHILD_ROLLOUT_REL, '{"unrelated":true}\n');
    const session = await resumeIntoTarget(createProfileClient(targetHome), sourceHome);
    try {
      await expect(readFile(join(targetHome, CHILD_ROLLOUT_REL))).rejects.toMatchObject({
        code: "ENOENT",
      });
    } finally {
      await session.close();
    }
  });

  test.each([
    { type: "sub_agent_activity", agent_thread_id: CHILD_THREAD_ID, kind: "started" },
    {
      type: "item_completed",
      item: { type: "SubAgentActivity", agent_thread_id: CHILD_THREAD_ID, kind: "completed" },
    },
  ])("copies V2 child history from native activity %j", async (activity) => {
    const { sourceHome, targetHome } = await createHomes();
    const records = [
      {
        type: "response_item",
        payload: { type: "function_call", name: "spawn_agent", call_id: "v2" },
      },
      {
        type: "response_item",
        payload: {
          type: "function_call_output",
          call_id: "v2",
          output: JSON.stringify({ task_name: "child" }),
        },
      },
      { type: "event_msg", payload: activity },
    ];
    await writeRollout(
      sourceHome,
      ROOT_ROLLOUT_REL,
      records.map((record) => JSON.stringify(record)).join("\n") + "\n",
    );
    await writeRollout(sourceHome, CHILD_ROLLOUT_REL, '{"child":true}\n');
    const session = await resumeIntoTarget(createProfileClient(targetHome), sourceHome);
    try {
      expect(await readFile(join(targetHome, CHILD_ROLLOUT_REL), "utf8")).toBe('{"child":true}\n');
    } finally {
      await session.close();
    }
  });

  test("copies native spawn outputs and recursive child histories without cycles", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const grandchildId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
    const grandchildRel = CHILD_ROLLOUT_REL.replace(CHILD_THREAD_ID, grandchildId);
    const records = [
      { type: "session_meta", payload: { id: ROOT_THREAD_ID } },
      {
        type: "response_item",
        payload: { type: "function_call", name: "spawn_agent", call_id: "spawn-one" },
      },
      {
        type: "response_item",
        payload: {
          type: "function_call_output",
          call_id: "spawn-one",
          output: JSON.stringify({ agent_id: CHILD_THREAD_ID }),
        },
      },
    ];
    await writeRollout(
      sourceHome,
      ROOT_ROLLOUT_REL,
      records.map((record) => JSON.stringify(record)).join("\n") + "\n",
    );
    const child =
      JSON.stringify({
        type: "event_msg",
        payload: { type: "collab_agent_spawn_end", new_thread_id: grandchildId },
      }) + "\n";
    const grandchild =
      JSON.stringify({
        type: "response_item",
        payload: { type: "agent", agentThreadId: ROOT_THREAD_ID },
      }) + "\n";
    await writeRollout(sourceHome, CHILD_ROLLOUT_REL, child);
    await writeRollout(sourceHome, grandchildRel, grandchild);
    const session = await resumeIntoTarget(createProfileClient(targetHome), sourceHome);
    try {
      expect(await readFile(join(targetHome, CHILD_ROLLOUT_REL), "utf8")).toBe(child);
      expect(await readFile(join(targetHome, grandchildRel), "utf8")).toBe(grandchild);
    } finally {
      await session.close();
    }
  });

  test.each([0, 1_000_000, 2_200_000])(
    "detects a large conflicting copy at byte %i even when another range matches",
    async (offset) => {
      const { sourceHome, targetHome } = await createHomes();
      const source =
        rootRolloutContent() +
        JSON.stringify({
          type: "response_item",
          payload: { type: "message", content: "a".repeat(2_500_000) },
        }) +
        "\n";
      const target = source.slice(0, offset) + "X" + source.slice(offset + 1);
      await writeRollout(sourceHome, ROOT_ROLLOUT_REL, source);
      await writeRollout(targetHome, ROOT_ROLLOUT_REL, target);
      const session = await resumeIntoTarget(createProfileClient(targetHome), sourceHome);
      try {
        expect(await readFile(join(targetHome, ROOT_ROLLOUT_REL), "utf8")).toBe(source);
        const backupDir = join(targetHome, "paseo-profile-transfer-backups", ROOT_THREAD_ID);
        const [backup] = await readdir(backupDir);
        expect(await readFile(join(backupDir, backup!), "utf8")).toBe(target);
      } finally {
        await session.close();
      }
    },
  );

  test("replaces a stale prefix rollout when switching back to an earlier home", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const full = rootRolloutContent();
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, full);
    await writeRollout(targetHome, ROOT_ROLLOUT_REL, full.slice(0, full.length / 2));

    const client = createProfileClient(targetHome);
    const session = await resumeIntoTarget(client, sourceHome);
    try {
      await expect(readFile(join(targetHome, ROOT_ROLLOUT_REL), "utf8")).resolves.toBe(full);
    } finally {
      await session.close();
    }
  });

  test("keeps a longer target rollout that already contains the session history", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const sourceContent = rootRolloutContent();
    const longerTarget = sourceContent + '{"appended":true}\n';
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, sourceContent);
    await writeRollout(targetHome, ROOT_ROLLOUT_REL, longerTarget);

    const client = createProfileClient(targetHome);
    const session = await resumeIntoTarget(client, sourceHome);
    try {
      await expect(readFile(join(targetHome, ROOT_ROLLOUT_REL), "utf8")).resolves.toBe(
        longerTarget,
      );
    } finally {
      await session.close();
    }
  });
});
