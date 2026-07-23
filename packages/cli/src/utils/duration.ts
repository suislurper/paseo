/**
 * Parse duration string to milliseconds.
 * Supports formats like: 500ms, 5m, 30s, 1h, 2h30m, 90, etc.
 * If no unit is specified, assumes seconds.
 */
export function parseDuration(input: string): number {
  const trimmed = input.trim();

  // If it's just a number, treat as seconds
  if (/^\d+$/.test(trimmed)) {
    return parseInt(trimmed, 10) * 1000;
  }

  // Parse duration with units
  let totalMs = 0;
  const regex = /(\d+)(ms|[smh])/g;
  let match;
  let hasMatch = false;
  let consumedUntil = 0;

  while ((match = regex.exec(trimmed)) !== null) {
    if (match.index !== consumedUntil) {
      throw new Error(
        `Invalid duration format: ${input}. Use formats like: 500ms, 5m, 30s, 1h, 2h30m`,
      );
    }
    hasMatch = true;
    const value = parseInt(match[1], 10);
    const unit = match[2];
    consumedUntil = regex.lastIndex;

    switch (unit) {
      case "ms":
        totalMs += value;
        break;
      case "s":
        totalMs += value * 1000;
        break;
      case "m":
        totalMs += value * 60 * 1000;
        break;
      case "h":
        totalMs += value * 60 * 60 * 1000;
        break;
    }
  }

  if (!hasMatch || consumedUntil !== trimmed.length) {
    throw new Error(
      `Invalid duration format: ${input}. Use formats like: 500ms, 5m, 30s, 1h, 2h30m`,
    );
  }

  return totalMs;
}
