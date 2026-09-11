import { expect, test } from "vitest";
import type { WorkspaceRegistry } from "./workspace-registry.js";
import {
  readAfterWorkspaceArchives,
  trackWorkspaceArchiveRequest,
} from "./workspace-archive-flight.js";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function registry(): WorkspaceRegistry {
  return {} as WorkspaceRegistry;
}

test("registers before operation starts and blocks another session's active read until teardown ends", async () => {
  const shared = registry();
  const teardown = deferred();
  let archived = false;
  let readStarted = false;
  const archive = trackWorkspaceArchiveRequest(shared, " workspace ", async () => {
    await teardown.promise;
    archived = true;
  });
  const read = readAfterWorkspaceArchives(
    shared,
    async () => {
      readStarted = true;
      return archived;
    },
    "workspace",
  );
  await Promise.resolve();
  expect(readStarted).toBe(false);
  teardown.resolve();
  await archive;
  await expect(read).resolves.toBe(true);
});

test("failed terminal requests release the barrier without replay", async () => {
  const shared = registry();
  const teardown = deferred();
  let calls = 0;
  const archive = trackWorkspaceArchiveRequest(shared, "workspace", async () => {
    calls += 1;
    await teardown.promise;
    throw new Error("teardown failed");
  });
  const failure = expect(archive).rejects.toThrow("teardown failed");
  const read = readAfterWorkspaceArchives(shared, async () => "active");
  teardown.resolve();
  await failure;
  await expect(read).resolves.toBe("active");
  expect(calls).toBe(1);
});

test("discards an active snapshot when an archive starts and finishes during its read", async () => {
  const shared = registry();
  const reading = deferred();
  const finishRead = deferred();
  let archived = false;
  let reads = 0;
  const read = readAfterWorkspaceArchives(shared, async () => {
    reads += 1;
    const snapshot = archived;
    if (reads === 1) {
      reading.resolve();
      await finishRead.promise;
    }
    return snapshot;
  });
  await reading.promise;
  await trackWorkspaceArchiveRequest(shared, "workspace", async () => {
    archived = true;
  });
  finishRead.resolve();
  await expect(read).resolves.toBe(true);
  expect(reads).toBe(2);
});
