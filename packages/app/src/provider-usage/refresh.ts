import type { QueryClient } from "@tanstack/react-query";
import type { DaemonClient } from "@getpaseo/client/internal/daemon-client";
import { providerUsageQueryKey } from "./query-key";

type ProviderUsageClient = Pick<DaemonClient, "listProviderUsage">;

export async function refreshProviderUsageCache(input: {
  queryClient: QueryClient;
  queryKey: ReturnType<typeof providerUsageQueryKey>;
  client: ProviderUsageClient;
  forceRefresh: boolean;
}): Promise<void> {
  await input.queryClient.cancelQueries({ queryKey: input.queryKey });
  const payload = await input.client.listProviderUsage({
    forceRefresh: input.forceRefresh,
  });
  input.queryClient.setQueryData(input.queryKey, payload);
}
