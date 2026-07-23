import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { providerUsageQueryKey } from "./query-key";
import { refreshProviderUsageCache } from "./refresh";

const PROVIDER_USAGE_STALE_TIME_MS = 5 * 60 * 1000;

describe("refreshProviderUsageCache", () => {
  it("forces exactly one request while the mounted query cache is fresh", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const queryKey = providerUsageQueryKey("server-1");
    const initial = {
      requestId: "initial",
      fetchedAt: "2026-07-23T00:00:00.000Z",
      providers: [],
    };
    const refreshed = {
      requestId: "refreshed",
      fetchedAt: "2026-07-23T00:01:00.000Z",
      providers: [],
    };
    const listProviderUsage = vi.fn(async () => refreshed);
    queryClient.setQueryData(queryKey, initial);

    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn: () => listProviderUsage(),
      staleTime: PROVIDER_USAGE_STALE_TIME_MS,
    });
    const unsubscribe = observer.subscribe(() => {});

    await refreshProviderUsageCache({
      queryClient,
      queryKey,
      client: { listProviderUsage },
      forceRefresh: true,
    });

    expect(listProviderUsage).toHaveBeenCalledTimes(1);
    expect(listProviderUsage).toHaveBeenCalledWith({ forceRefresh: true });
    expect(queryClient.getQueryData(queryKey)).toEqual(refreshed);

    unsubscribe();
  });
});
