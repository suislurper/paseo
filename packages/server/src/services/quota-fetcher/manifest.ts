import type {
  ProviderUsageFetcher,
  ProviderUsageFetcherFactoryOptions,
  ProviderUsageFetcherManifestEntry,
} from "./provider.js";
import { ClaudeQuotaProvider } from "./providers/claude.js";
import { CodexQuotaProvider } from "./providers/codex.js";
import { CopilotQuotaProvider } from "./providers/copilot.js";
import { CursorQuotaProvider } from "./providers/cursor.js";
import { GrokQuotaProvider } from "./providers/grok.js";
import { KimiQuotaProvider } from "./providers/kimi.js";
import { MiniMaxQuotaProvider } from "./providers/minimax.js";
import { ZaiQuotaProvider } from "./providers/zai.js";

export const PROVIDER_USAGE_FETCHERS: readonly ProviderUsageFetcherManifestEntry[] = [
  {
    providerId: "claude",
    create: (options) =>
      new ClaudeQuotaProvider({
        logger: options.logger,
        fetch: options.fetch,
      }),
  },
  {
    providerId: "codex",
    create: (options) =>
      new CodexQuotaProvider({
        logger: options.logger,
        fetch: options.fetch,
      }),
  },
  {
    providerId: "copilot",
    create: (options) => new CopilotQuotaProvider({ logger: options.logger, fetch: options.fetch }),
  },
  {
    providerId: "cursor",
    create: (options) => new CursorQuotaProvider({ logger: options.logger, fetch: options.fetch }),
  },
  {
    providerId: "zai",
    create: (options) => new ZaiQuotaProvider({ logger: options.logger, fetch: options.fetch }),
  },
  {
    providerId: "grok",
    create: (options) => new GrokQuotaProvider({ logger: options.logger, fetch: options.fetch }),
  },
  {
    providerId: "kimi",
    create: (options) => new KimiQuotaProvider({ logger: options.logger, fetch: options.fetch }),
  },
  {
    providerId: "minimax",
    create: (options) => new MiniMaxQuotaProvider({ logger: options.logger, fetch: options.fetch }),
  },
];

export function createProviderUsageFetchers(
  options: ProviderUsageFetcherFactoryOptions,
): ProviderUsageFetcher[] {
  return PROVIDER_USAGE_FETCHERS.map((entry) => entry.create(options));
}

// Minimal structural view of an `agents.providers` override entry, kept local
// so the quota fetchers do not depend on the server config schema.
export interface ProviderUsageProfile {
  extends?: string;
  label?: string;
  enabled?: boolean;
  env?: Record<string, string>;
}

// Custom provider profiles that extend claude/codex and point at their own
// config home get a dedicated usage fetcher for that home, so every account
// shows up in the usage panel — not just the default login.
export function createProfileUsageFetchers(
  profiles: Record<string, ProviderUsageProfile> | undefined,
  options: ProviderUsageFetcherFactoryOptions,
): ProviderUsageFetcher[] {
  const fetchers: ProviderUsageFetcher[] = [];
  for (const [providerId, profile] of Object.entries(profiles ?? {})) {
    if (profile.enabled === false) continue;
    const displayName = profile.label ?? providerId;
    if (profile.extends === "claude") {
      const claudeHome = profile.env?.["CLAUDE_CONFIG_DIR"] ?? profile.env?.["CLAUDE_HOME"];
      if (!claudeHome) continue;
      fetchers.push(
        new ClaudeQuotaProvider({
          logger: options.logger,
          fetch: options.fetch,
          claudeHome,
          providerId,
          displayName,
        }),
      );
    } else if (profile.extends === "codex") {
      const codexHome = profile.env?.["CODEX_HOME"];
      if (!codexHome) continue;
      fetchers.push(
        new CodexQuotaProvider({
          logger: options.logger,
          fetch: options.fetch,
          codexHome,
          providerId,
          displayName,
        }),
      );
    }
  }
  return fetchers;
}
