import type { Logger } from "pino";
import type { ProviderUsage } from "../../server/messages.js";
import { createProfileUsageFetchers, createProviderUsageFetchers } from "./manifest.js";
import type { ProviderUsageProfile } from "./manifest.js";
import type { ProviderApiFetch, ProviderUsageFetcher } from "./provider.js";
import { unavailableUsage } from "./usage.js";

export interface ProviderUsageServiceOptions {
  logger: Logger;
  fetchers?: ProviderUsageFetcher[];
  fetch?: ProviderApiFetch;
  cacheTtlMs?: number;
  now?: () => number;
  // Live view of `agents.providers` so custom claude/codex profiles get their
  // own usage entries. Read on every refresh to follow config changes.
  providerProfiles?: () => Record<string, ProviderUsageProfile> | undefined;
}

export interface ProviderUsageListResult {
  fetchedAt: string;
  providers: ProviderUsage[];
}

const DEFAULT_PROVIDER_USAGE_CACHE_TTL_MS = 5 * 60 * 1000;

export class ProviderUsageService {
  private readonly logger: Logger;
  private readonly fetchers: ProviderUsageFetcher[];
  private readonly fetchApi: ProviderApiFetch | undefined;
  private readonly providerProfiles?: () => Record<string, ProviderUsageProfile> | undefined;
  private readonly cacheTtlMs: number;
  private readonly now: () => number;
  private generation = 0;
  private cached: {
    generation: number;
    fetchedAtMs: number;
    result: ProviderUsageListResult;
  } | null = null;
  private inFlight: {
    generation: number;
    request: Promise<ProviderUsageListResult>;
  } | null = null;

  constructor(options: ProviderUsageServiceOptions) {
    this.logger = options.logger.child({ module: "provider-usage-service" });
    this.fetchApi = options.fetch;
    this.providerProfiles = options.providerProfiles;
    this.fetchers =
      options.fetchers ??
      createProviderUsageFetchers({
        logger: this.logger,
        fetch: options.fetch,
      });
    this.cacheTtlMs = options.cacheTtlMs ?? DEFAULT_PROVIDER_USAGE_CACHE_TTL_MS;
    this.now = options.now ?? Date.now;
  }

  async listUsage(options?: { forceRefresh?: boolean }): Promise<ProviderUsageListResult> {
    const nowMs = this.now();
    const generation = this.generation;
    if (
      !options?.forceRefresh &&
      this.cached &&
      this.cached.generation === generation &&
      nowMs - this.cached.fetchedAtMs < this.cacheTtlMs
    ) {
      return this.cached.result;
    }

    if (this.inFlight?.generation === generation) {
      return this.inFlight.request;
    }

    const request = this.fetchFreshUsage(nowMs, generation);
    const inFlight = { generation, request };
    this.inFlight = inFlight;
    try {
      return await request;
    } finally {
      if (this.inFlight === inFlight) {
        this.inFlight = null;
      }
    }
  }

  invalidate(): void {
    this.generation += 1;
    this.cached = null;
  }

  private resolveFetchers(): ProviderUsageFetcher[] {
    const profileFetchers = createProfileUsageFetchers(this.providerProfiles?.(), {
      logger: this.logger,
      fetch: this.fetchApi,
    });
    if (profileFetchers.length === 0) {
      return this.fetchers;
    }
    // A profile id can never shadow a base fetcher (custom provider ids are
    // distinct from built-in ids), but guard against duplicates regardless.
    const baseIds = new Set(this.fetchers.map((fetcher) => fetcher.providerId));
    return [
      ...this.fetchers,
      ...profileFetchers.filter((fetcher) => !baseIds.has(fetcher.providerId)),
    ];
  }

  private async fetchFreshUsage(
    nowMs: number,
    generation: number,
  ): Promise<ProviderUsageListResult> {
    const fetchers = this.resolveFetchers();
    const settled = await Promise.allSettled(fetchers.map((fetcher) => fetcher.fetchUsage()));
    const providers = settled.map((result, index) => {
      const fetcher = fetchers[index];
      if (result.status === "fulfilled") {
        return result.value;
      }
      this.logger.debug(
        { err: result.reason, providerId: fetcher.providerId },
        "Provider usage fetch failed",
      );
      return unavailableUsage({
        providerId: fetcher.providerId,
        displayName: fetcher.displayName,
        iconProviderId: fetcher.iconProviderId,
        error: result.reason instanceof Error ? result.reason.message : String(result.reason),
      });
    });

    const result = { fetchedAt: new Date(nowMs).toISOString(), providers };
    if (this.generation === generation) {
      this.cached = { generation, fetchedAtMs: nowMs, result };
    }
    return result;
  }
}
