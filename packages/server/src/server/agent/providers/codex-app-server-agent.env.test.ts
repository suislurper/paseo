import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, test } from "vitest";

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

  test("refuses to overwrite a diverged rollout in the target home", async () => {
    const { sourceHome, targetHome } = await createHomes();
    const sourceContent = rootRolloutContent();
    await writeRollout(sourceHome, ROOT_ROLLOUT_REL, sourceContent);
    // Same length, different bytes: neither file is a prefix of the other.
    await writeRollout(targetHome, ROOT_ROLLOUT_REL, `X${sourceContent.slice(1)}`);

    const client = createProfileClient(targetHome);
    await expect(resumeIntoTarget(client, sourceHome)).rejects.toThrow(
      "has diverged between profile homes",
    );
  });

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
