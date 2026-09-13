import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderDirectorViewRoute } from "./testUtils";
import * as api from "../api";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    checkAccess: vi.fn(),
    listSecretsForWorkspace: vi.fn(),
    listSecretHolders: vi.fn(),
    disclosureTimeline: vi.fn(),
    agentBeliefs: vi.fn(),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.checkAccess).mockResolvedValue({ can_inspect: true });
  vi.mocked(api.listSecretsForWorkspace).mockResolvedValue([]);
  vi.mocked(api.listSecretHolders).mockResolvedValue([]);
  vi.mocked(api.disclosureTimeline).mockResolvedValue([]);
  vi.mocked(api.agentBeliefs).mockResolvedValue([]);
});

describe("test_audit_indicator_present_on_all_director_view_routes", () => {
  it.each([
    ["landing (secrets by holder)", "/workspaces/ws-1/director-view"],
    ["disclosure timeline", "/workspaces/ws-1/director-view/secrets/secret-1/timeline"],
    ["agent beliefs", "/workspaces/ws-1/director-view/agents/agent-1/beliefs"],
  ])("shows the persistent audit indicator on the %s route", async (_label, path) => {
    renderDirectorViewRoute(path);

    expect(await screen.findByTestId("audit-indicator")).toBeInTheDocument();
  });

  it("hides the audit indicator's sibling content, but never the indicator itself, when access is denied", async () => {
    vi.mocked(api.checkAccess).mockResolvedValue({ can_inspect: false });

    renderDirectorViewRoute("/workspaces/ws-1/director-view");

    expect(await screen.findByTestId("audit-indicator")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(/do not have permission/i);
  });
});
