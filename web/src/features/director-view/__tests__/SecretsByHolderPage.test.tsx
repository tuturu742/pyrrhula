import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useDirectorViewStore } from "@/stores/directorView";
import { renderDirectorViewPage } from "./testUtils";
import * as api from "../api";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    listSecretsForWorkspace: vi.fn(),
    listSecretHolders: vi.fn(),
    inspectSecret: vi.fn(),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  useDirectorViewStore.setState({ inspectionCount: 0 });
});

describe("SecretsByHolderPage", () => {
  it("test_each_plaintext_expansion_writes_exactly_one_audit_row", async () => {
    vi.mocked(api.listSecretsForWorkspace).mockResolvedValue([
      {
        id: "secret-1",
        workspace_id: "ws-1",
        subject_kind: "entity",
        subject_id: "entity-1",
        gist: "a hidden fact",
        disclosure_state: "concealed",
        scope_key: "workspace_public",
        publication: "guarded",
        version: 1,
        content: null,
        hint_text: null,
        behavioral_directive: null,
        is_author: false,
      },
    ]);
    vi.mocked(api.listSecretHolders).mockResolvedValue([
      { holder_principal_id: "agent-1", holder_kind: "told" },
    ]);
    vi.mocked(api.inspectSecret).mockResolvedValue({
      id: "secret-1",
      subject_kind: "entity",
      subject_id: "entity-1",
      content: "the actual plaintext",
      gist: "a hidden fact",
      hint_text: null,
      behavioral_directive: null,
      disclosure_state: "concealed",
    });

    renderDirectorViewPage("/workspaces/ws-1/director-view");

    const revealButton = await screen.findByRole("button", { name: /reveal plaintext/i });

    // A second render pass (e.g. React strict-mode double-invoke of an effect) must not
    // itself cause a second inspect() call -- only a deliberate click does.
    expect(api.inspectSecret).not.toHaveBeenCalled();
    expect(useDirectorViewStore.getState().inspectionCount).toBe(0);

    await userEvent.click(revealButton);

    await waitFor(() => expect(screen.getByText("the actual plaintext")).toBeInTheDocument());

    // Exactly one expansion click -> exactly one inspectSecret() call (one audit row on
    // the backend, per OverseerService.inspect()) -> exactly one counter increment.
    expect(api.inspectSecret).toHaveBeenCalledTimes(1);
    expect(useDirectorViewStore.getState().inspectionCount).toBe(1);

    await userEvent.click(revealButton);
    await waitFor(() => expect(api.inspectSecret).toHaveBeenCalledTimes(2));
    expect(useDirectorViewStore.getState().inspectionCount).toBe(2);
  });

  it("test_non_overseer_gets_permission_error_not_empty_lists", async () => {
    vi.mocked(api.listSecretsForWorkspace).mockRejectedValue(new api.PermissionDeniedError());

    renderDirectorViewPage("/workspaces/ws-1/director-view");

    expect(await screen.findByRole("alert")).toHaveTextContent(/do not have permission/i);
    // The absence of data is itself information -- a denied principal must never see the
    // page's own "no secrets yet" empty state, which would look identical to "this
    // workspace genuinely has none".
    expect(screen.queryByText(/no .*yet/i)).not.toBeInTheDocument();
  });
});
