import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** Terminal states of the swdev `work_item` lifecycle: nothing follows them, so an item
 * here is done being worked and only clutters the list it is still offered in. */
const FINISHED_STATES = new Set(["merged", "done"]);

/** the human half, finally on screen: the session's work items with their FSM
 * status, delegate selected ones to coding agents, and approve / request changes on
 * items that came back for review. The endpoints existed with zero UI — the whole
 * review loop ran over curl. Shown only when the session has repos bound. */
export function WorkPanel({
  sessionId,
  workspaceId,
}: {
  sessionId: string;
  workspaceId: string | undefined;
}) {
  const { data: currentWorkflow } = useQuery({
    queryKey: ["workflow-current"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workflows/current");
      if (error) throw error;
      return data;
    },
  });
  const hasRepos = Boolean(currentWorkflow?.workflow?.repo_access);
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [showCompleted, setShowCompleted] = useState(false);
  // A session leads with its own work. The workspace backlog is real and shared, but it
  // accumulates: eight runs left thirty items, most of them stuck mid-lifecycle, and the
  // six this session filed were indistinguishable in the list.
  const [showBacklog, setShowBacklog] = useState(false);
  const [reviewFor, setReviewFor] = useState<string | null>(null);
  const [reviewBranch, setReviewBranch] = useState("");
  const [reviewComment, setReviewComment] = useState("Please address review feedback.");

  const items = useQuery({
    queryKey: ["work-items", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/entities", {
        params: { query: { workspace_id: workspaceId!, schema_key: "work_item" } },
      });
      if (error) throw error;
      return data;
    },
    enabled: Boolean(workspaceId) && hasRepos,
    refetchInterval: 15000,
  });

  const delegate = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/sessions/{session_id}/delegate", {
        params: { path: { session_id: sessionId } },
        body: { work_item_ids: [...selected], server_key: "git", auto_review: true },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Delegated — agents are on it. Watch the transcript for updates.");
      setSelected(new Set());
    },
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Delegation failed.")),
  });

  const review = useMutation({
    mutationFn: async (workItemId: string) => {
      const { error } = await apiClient.POST("/sessions/{session_id}/review", {
        params: { path: { session_id: sessionId } },
        body: {
          work_item_id: workItemId,
          branch: reviewBranch.trim(),
          comment: reviewComment.trim() || "Please address review feedback.",
          server_key: "git",
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Change request sent back to the agent.");
      setReviewFor(null);
      setReviewBranch("");
      void queryClient.invalidateQueries({ queryKey: ["work-items", workspaceId] });
    },
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Review request failed.")),
  });

  if (!hasRepos || !workspaceId) return null;
  const workItems = items.data ?? [];
  if (items.isSuccess && workItems.length === 0) return null;

  // The machine is keyed `lifecycle`; this used to look up `status`, which no schema
  // declares, and fell through to whatever happened to be first. Read the real key, keep
  // the old guess as a fallback for a schema that names its machine differently, and only
  // then give up. An item that has never transitioned has no stored state at all, so the
  // machine's own starting point is what to show rather than an em dash.
  const status = (fsm: Record<string, string>) =>
    fsm["lifecycle"] ?? fsm["status"] ?? Object.values(fsm)[0] ?? "backlog";

  // Work items belong to the workspace, not to one session -- a backlog is shared, and a
  // standup, a triage and a planning session all legitimately look at the same one. Two
  // things made that unreadable: finished work never leaving the list, and every other
  // session's work sitting in it. Both are hidden by default and both say how much they
  // are hiding, because a filter you cannot see is a filter that loses your work.
  const isFinished = (item: { fsm_states: Record<string, string> }) =>
    FINISHED_STATES.has(status(item.fsm_states));
  const isThisSession = (item: { origin_session_id?: string | null }) =>
    item.origin_session_id === sessionId;
  const backlogCount = workItems.filter((i) => !isThisSession(i) && !isFinished(i)).length;
  const inScope = showBacklog ? workItems : workItems.filter(isThisSession);
  const finishedCount = inScope.filter(isFinished).length;
  const visibleItems = showCompleted ? inScope : inScope.filter((i) => !isFinished(i));

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium">Work items</h2>
        <span className="flex items-center gap-2">
          {backlogCount > 0 && (
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={showBacklog}
                onChange={(e) => setShowBacklog(e.target.checked)}
              />
              Workspace backlog ({backlogCount})
            </label>
          )}
          {finishedCount > 0 && (
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={showCompleted}
                onChange={(e) => setShowCompleted(e.target.checked)}
              />
              Show completed ({finishedCount})
            </label>
          )}
          <Button
          size="sm"
          disabled={selected.size === 0 || delegate.isPending}
          onClick={() => delegate.mutate()}
        >
            Delegate {selected.size > 0 ? `(${selected.size})` : ""}
          </Button>
        </span>
      </div>
      <ul className="flex flex-col gap-1.5">
        {visibleItems.map((item) => (
          <li key={item.id} className="flex flex-col gap-1.5 text-sm">
            <div className="flex items-center justify-between gap-2">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={selected.has(item.id)}
                  onChange={(e) => {
                    const next = new Set(selected);
                    if (e.target.checked) next.add(item.id);
                    else next.delete(item.id);
                    setSelected(next);
                  }}
                />
                {item.name}
              </label>
              <span className="flex items-center gap-2">
                <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                  {status(item.fsm_states)}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setReviewFor(reviewFor === item.id ? null : item.id)}
                >
                  Request changes
                </Button>
              </span>
            </div>
            {reviewFor === item.id && (
              <form
                className="flex flex-wrap items-center gap-2 pl-6"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (reviewBranch.trim()) review.mutate(item.id);
                }}
              >
                <Input
                  placeholder="branch (e.g. work/abc123)"
                  className="w-52"
                  value={reviewBranch}
                  onChange={(e) => setReviewBranch(e.target.value)}
                />
                <Input
                  placeholder="what should change?"
                  className="w-64"
                  value={reviewComment}
                  onChange={(e) => setReviewComment(e.target.value)}
                />
                <Button
                  type="submit"
                  variant="outline"
                  size="sm"
                  disabled={!reviewBranch.trim() || review.isPending}
                >
                  Send
                </Button>
              </form>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
