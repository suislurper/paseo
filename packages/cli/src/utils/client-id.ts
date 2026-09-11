import { randomUUID } from "node:crypto";

let cachedCliClientId: string | null = null;

function generateCliClientId(): string {
  return `cid_${randomUUID().replace(/-/g, "")}`;
}

export function getCliClientId(): string {
  if (!cachedCliClientId) {
    cachedCliClientId = generateCliClientId();
  }
  return cachedCliClientId;
}
