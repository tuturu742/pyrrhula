import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EntitySheetContainer } from "../EntitySheetContainer";
import * as api from "../api";
import * as sseModule from "@/lib/sse/useSSE";
import type { EntityView } from "../types";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, getEntityView: vi.fn() };
});

vi.mock("@/lib/sse/useSSE", async () => {
  const actual = await vi.importActual<typeof import("@/lib/sse/useSSE")>("@/lib/sse/useSSE");
  return { ...actual, useSSE: vi.fn() };
});

function entityWithName(name: string): EntityView {
  return {
    id: "entity-1",
    key: "hero-1",
    name: "A Hero",
    schema_id: "schema-1",
    version: 1,
    fields: [{ key: "name", type: "string", value: name, tags: ["identity"], tag_metadata: {} }],
    derived: {},
    fsm_states: {},
    views: [],
  };
}

describe("EntitySheetContainer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("test_sheet_updates_live_on_entity_state_change_events", async () => {
    vi.mocked(api.getEntityView)
      .mockResolvedValueOnce(entityWithName("Before Update"))
      .mockResolvedValueOnce(entityWithName("After Update"));

    let currentEvents: sseModule.SSEMessageEvent[] = [];
    vi.mocked(sseModule.useSSE).mockImplementation(() => ({
      liveText: "",
      events: currentEvents,
      typing: null,
    connected: true,
      status: "live" as const,
    }));

    // One QueryClient, one mounted provider tree, for the whole test -- `rerender`
    // updates the same subtree in place (as a real live session would), rather than
    // remounting a fresh app on every SSE event.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const props = {
      workspaceId: "ws-1",
      entityId: "entity-1",
      labelKeyPrefix: "schema.test",
      sessionId: "session-1",
    };

    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <EntitySheetContainer {...props} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByText("Before Update")).toBeInTheDocument());
    expect(api.getEntityView).toHaveBeenCalledTimes(1);

    // A live-session event for this exact entity arrives -- without a reload, the
    // sheet refetches and shows the new state.
    currentEvents = [{ kind: "entity_state_changed", payload: { entity_id: "entity-1" } }];
    rerender(
      <QueryClientProvider client={queryClient}>
        <EntitySheetContainer {...props} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByText("After Update")).toBeInTheDocument());
    expect(api.getEntityView).toHaveBeenCalledTimes(2);
  });

  it("ignores an entity_state_changed event for a different entity", async () => {
    vi.mocked(api.getEntityView).mockResolvedValue(entityWithName("Unchanged"));

    let currentEvents: sseModule.SSEMessageEvent[] = [];
    vi.mocked(sseModule.useSSE).mockImplementation(() => ({
      liveText: "",
      events: currentEvents,
      typing: null,
    connected: true,
      status: "live" as const,
    }));

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const props = {
      workspaceId: "ws-1",
      entityId: "entity-1",
      labelKeyPrefix: "schema.test",
      sessionId: "session-1",
    };

    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <EntitySheetContainer {...props} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Unchanged")).toBeInTheDocument());
    expect(api.getEntityView).toHaveBeenCalledTimes(1);

    currentEvents = [{ kind: "entity_state_changed", payload: { entity_id: "some-other-entity" } }];
    rerender(
      <QueryClientProvider client={queryClient}>
        <EntitySheetContainer {...props} />
      </QueryClientProvider>,
    );

    // Give any (wrongly-triggered) refetch a moment to happen, then confirm it didn't.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(api.getEntityView).toHaveBeenCalledTimes(1);
  });
});
