import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** The workspace's between-session clock (G4.x): read the current value, advance it.
 * Advancing fans out scheduled effects via a worker job — the API existed with no UI,
 * so time literally could not move from the app. */
export function ClockCard({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [target, setTarget] = useState("");

  const clock = useQuery({
    queryKey: ["workspace-clock", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/clock", {
        params: { path: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const advance = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/workspaces/{workspace_id}/clock", {
        params: { path: { workspace_id: workspaceId } },
        body: { to_value: Number(target) },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Clock advanced — scheduled effects are being applied.");
      setTarget("");
      void queryClient.invalidateQueries({ queryKey: ["workspace-clock", workspaceId] });
    },
    onError: (e) =>
      toast.error(
        String((e as { detail?: string })?.detail ?? "Could not advance the clock."),
      ),
  });

  if (clock.isError) return null; // clock is optional workflow furniture

  const current = clock.data?.clock_value ?? 0;
  const next = Number(target);
  return (
    <div className="flex items-center gap-3 rounded-md border border-border px-4 py-2">
      <span className="text-sm">
        Clock: <span className="font-mono">{current}</span>
      </span>
      <Input
        type="number"
        placeholder={String(current + 1)}
        className="w-24"
        value={target}
        onChange={(e) => setTarget(e.target.value)}
      />
      <Button
        variant="outline"
        size="sm"
        disabled={!target || Number.isNaN(next) || next <= current || advance.isPending}
        onClick={() => advance.mutate()}
      >
        Advance
      </Button>
    </div>
  );
}
