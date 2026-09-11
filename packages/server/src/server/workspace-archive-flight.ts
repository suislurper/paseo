import type { WorkspaceRegistry } from "./workspace-registry.js";

interface ArchiveFlights {
  generation: number;
  requests: Map<string, Set<Promise<void>>>;
}

// Sessions on one daemon share the registry, including after a client reconnects.
// This is a read barrier only: it neither retries nor coalesces mutations.
const flightsByRegistry = new WeakMap<WorkspaceRegistry, ArchiveFlights>();

function stateFor(registry: WorkspaceRegistry): ArchiveFlights {
  let state = flightsByRegistry.get(registry);
  if (!state) {
    state = { generation: 0, requests: new Map() };
    flightsByRegistry.set(registry, state);
  }
  return state;
}

export function trackWorkspaceArchiveRequest(
  registry: WorkspaceRegistry,
  workspaceId: string,
  operation: () => Promise<void>,
): Promise<void> {
  const state = stateFor(registry);
  const id = workspaceId.trim();
  const requests = state.requests.get(id) ?? new Set<Promise<void>>();
  // Defer operation until registration, including its initial registry lookup.
  const flight = Promise.resolve()
    .then(operation)
    .finally(() => {
      requests.delete(flight);
      if (requests.size === 0) state.requests.delete(id);
      state.generation += 1;
    });
  requests.add(flight);
  state.requests.set(id, requests);
  state.generation += 1;
  return flight;
}

export async function readAfterWorkspaceArchives<T>(
  registry: WorkspaceRegistry,
  read: () => Promise<T>,
  workspaceId?: string,
): Promise<T> {
  const state = stateFor(registry);
  const pending = () =>
    workspaceId === undefined
      ? [...state.requests.values()].flatMap((requests) => Array.from(requests))
      : [...(state.requests.get(workspaceId.trim()) ?? [])];
  while (true) {
    await Promise.allSettled(pending());
    if (pending().length > 0) continue;
    const generation = state.generation;
    const result = await read();
    // Also catches requests that started AND finished during the asynchronous read.
    // A registry-wide generation is conservative for a single-workspace inspection.
    if (state.generation === generation) return result;
  }
}
