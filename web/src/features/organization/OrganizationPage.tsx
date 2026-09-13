import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { useMe } from "@/features/admin/useMe";
import { UsageLimitsCard } from "@/features/workspaces/UsageLimitsCard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const ROLES = ["participant", "editor", "admin", "owner", "viewer"] as const;

/** Owner-facing organization management: the people in your org (create, deactivate),
 * plus org-wide settings (daily usage caps). Until this page existed, creating a user
 * required the PLATFORM admin console — an org owner could not add their own teammate. */
export function OrganizationPage() {
  const queryClient = useQueryClient();
  const { data: me } = useMe();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<(typeof ROLES)[number]>("participant");

  const users = useQuery({
    queryKey: ["tenant-users"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tenant/users");
      if (error) throw error;
      return data;
    },
    retry: false,
  });

  const create = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/tenant/users", {
        body: {
          email: email.trim(),
          password,
          display_name: displayName.trim() || email.split("@")[0],
          role,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("User created — share the password with them securely.");
      setEmail("");
      setPassword("");
      setDisplayName("");
      void queryClient.invalidateQueries({ queryKey: ["tenant-users"] });
    },
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Could not create the user.")),
  });

  const toggle = useMutation({
    mutationFn: async ({ id, disabled }: { id: string; disabled: boolean }) => {
      const path = disabled
        ? "/tenant/users/{principal_id}/reactivate"
        : "/tenant/users/{principal_id}/deactivate";
      const { error } = await apiClient.POST(path, {
        params: { path: { principal_id: id } },
      });
      if (error) throw error;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["tenant-users"] }),
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Change failed.")),
  });

  if (users.isError) {
    return (
      <div className="flex flex-col gap-2 py-8">
        <h1 className="text-xl font-semibold">Organization</h1>
        <p className="text-sm text-muted-foreground">
          Managing the organization needs an owner or admin role.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">Organization</h1>
        <p className="text-sm text-muted-foreground">
          {me?.tenant_name || me?.tenant_slug} — people and org-wide settings.
        </p>
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-border p-4">
        <h2 className="text-sm font-medium">People</h2>
        {users.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
        <ul className="flex flex-col gap-1.5">
          {(users.data ?? []).map((u) => (
            <li
              key={u.principal_id}
              className="flex items-center justify-between gap-2 text-sm"
            >
              <span className={u.disabled ? "opacity-50" : undefined}>
                {u.display_name}
                {u.email && (
                  <span className="ml-2 text-xs text-muted-foreground">{u.email}</span>
                )}
                {u.disabled && (
                  <span className="ml-2 text-xs text-destructive">deactivated</span>
                )}
              </span>
              <span className="flex items-center gap-2">
                <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                  {u.role}
                </span>
                {u.principal_id !== me?.principal_id && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      toggle.mutate({ id: u.principal_id, disabled: u.disabled })
                    }
                  >
                    {u.disabled ? "Reactivate" : "Deactivate"}
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
        <form
          className="flex flex-wrap items-center gap-2 border-t border-border pt-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (email.trim() && password.length >= 8) create.mutate();
          }}
        >
          <Input
            type="email"
            placeholder="email"
            className="w-56"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <Input
            placeholder="display name"
            className="w-40"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
          <Input
            type="password"
            placeholder="initial password (min 8)"
            className="w-52"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <select
            className="rounded-md border border-input bg-transparent px-2 py-1.5 text-sm"
            value={role}
            onChange={(e) => setRole(e.target.value as (typeof ROLES)[number])}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <Button
            type="submit"
            variant="outline"
            disabled={!email.trim() || password.length < 8 || create.isPending}
          >
            Create user
          </Button>
        </form>
      </div>

      <UsageLimitsCard />
    </div>
  );
}
