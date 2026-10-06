import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/features/previews/usePreviews", () => ({
  absoluteUrl: (u: string) => u,
  usePreviews: () => ({ data: [] }),
  usePreviewActions: () => ({
    deploy: { mutate: vi.fn(), isPending: false },
    share: { mutate: vi.fn(), isPending: false },
    stop: { mutate: vi.fn(), isPending: false },
  }),
  useRepoPullRequests: () => ({
    data: [{ branch: "pyr/45dd0469-3", pr_ref: "#11", title: "Build tested playable Mice Invaders", previewable: true }],
  }),
}));

import { RepoPreviewRow } from "../SessionView";

const repo = { id: "repo-1", key: "mice-invaders", name: "Mice Invaders", artifact_name: "web.tgz" };
const stoppedLatest = { id: "p-old", repo_id: "repo-1", git_ref: "", status: "stopped" };
const runningBranch = { id: "p-branch", repo_id: "repo-1", git_ref: "pyr/45dd0469-3", status: "running" };

type Preview = { id: string; repo_id: string; git_ref: string; status: string };

function renderRow(previews: Preview[]) {
  return render(<RepoPreviewRow repo={repo} sessionId="s-1" previews={previews} setNotice={() => {}} />);
}

describe("RepoPreviewRow", () => {
  it("shows the running branch preview, not an older stopped one for the latest build", () => {
    renderRow([stoppedLatest, runningBranch]);
    expect(screen.getByRole("combobox")).toHaveValue("pyr/45dd0469-3");
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("Copy link")).toBeInTheDocument();
  });

  it("keeps the latest build selected when that is what is running", () => {
    renderRow([{ ...stoppedLatest, status: "running" }, runningBranch]);
    expect(screen.getByRole("combobox")).toHaveValue("");
  });

  it("falls back to the latest build when nothing is running", () => {
    renderRow([stoppedLatest]);
    expect(screen.getByRole("combobox")).toHaveValue("");
    expect(screen.getByText("stopped")).toBeInTheDocument();
  });

  it("respects the choice once someone picks", async () => {
    renderRow([stoppedLatest, runningBranch]);
    await userEvent.selectOptions(screen.getByRole("combobox"), "");
    expect(screen.getByRole("combobox")).toHaveValue("");
    expect(screen.getByText("stopped")).toBeInTheDocument();
  });
});
