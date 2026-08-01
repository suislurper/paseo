import { describe, expect, it } from "vitest";
import {
  ScheduleCreateRequestSchema,
  ScheduleIdentityResponseSchema,
  ScheduleUpdateRequestSchema,
} from "./rpc-schemas.js";

describe("schedule RPC schemas", () => {
  it("accepts only the bounded schedule identity surface", () => {
    const parsed = ScheduleIdentityResponseSchema.parse({
      type: "schedule.identity.response",
      payload: {
        requestId: "request-identity",
        schedule: {
          id: "fb12e97c",
          cadence: { type: "cron", expression: "*/5 * * * *" },
          target: {
            type: "new-agent",
            provider: "codex",
            cwd: "/secret/workspace",
            systemPrompt: "secret system prompt",
          },
          status: "active",
          expiresAt: null,
          prompt: "secret wake prompt",
          runs: [{ output: "secret output" }],
        },
        error: null,
      },
    });

    expect(parsed.payload.schedule).toEqual({
      id: "fb12e97c",
      cadence: { type: "cron" },
      target: { type: "new-agent", provider: "codex" },
      status: "active",
      expiresAt: null,
    });
    expect(JSON.stringify(parsed)).not.toContain("secret");
  });

  it("round-trips new-agent run options on create requests", () => {
    expect(
      ScheduleCreateRequestSchema.parse({
        type: "schedule/create",
        requestId: "request-1",
        prompt: "Run the task",
        cadence: { type: "every", everyMs: 60_000 },
        target: {
          type: "new-agent",
          config: {
            provider: "claude",
            cwd: "/tmp/project",
            thinkingOptionId: "think-hard",
            archiveOnFinish: false,
            isolation: "worktree",
          },
        },
      }),
    ).toEqual({
      type: "schedule/create",
      requestId: "request-1",
      prompt: "Run the task",
      cadence: { type: "every", everyMs: 60_000 },
      target: {
        type: "new-agent",
        config: {
          provider: "claude",
          cwd: "/tmp/project",
          thinkingOptionId: "think-hard",
          archiveOnFinish: false,
          isolation: "worktree",
        },
      },
    });
  });

  it("round-trips new-agent run options on update requests", () => {
    expect(
      ScheduleUpdateRequestSchema.parse({
        type: "schedule/update",
        requestId: "request-1",
        scheduleId: "schedule-1",
        newAgentConfig: {
          thinkingOptionId: "think-hard",
          archiveOnFinish: false,
          isolation: "worktree",
        },
      }),
    ).toEqual({
      type: "schedule/update",
      requestId: "request-1",
      scheduleId: "schedule-1",
      newAgentConfig: {
        thinkingOptionId: "think-hard",
        archiveOnFinish: false,
        isolation: "worktree",
      },
    });
  });
});
