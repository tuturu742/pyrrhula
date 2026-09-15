import { Link, NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { MissingVocabularyKeysBadge } from "@/lib/vocabulary/MissingVocabularyKeysBadge";
import { AssistantWidget } from "@/features/agents/AssistantWidget";
import { useMe } from "@/features/admin/useMe";
import { UserMenu } from "@/components/UserMenu";

/** App shell: a full-width top bar — brand left, primary nav beside it, account actions
 * pinned right — over a wide content container. Nav entries highlight when active.
 * Platform admins (organization "admin") get the admin navigation instead — their
 * tenant has no workspaces to convene. */
export function AppShell() {
  const { data: currentWorkflow } = useQuery({
    queryKey: ["workflow-current"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workflows/current");
      if (error) throw error;
      return data;
    },
  });
  const repoAccess = Boolean(currentWorkflow?.workflow?.repo_access);
  const t = useLabel();
  const me = useMe();
  const isAdmin = me.data?.platform_admin === true;

  const links: Array<{ to: string; label: string; end?: boolean }> = isAdmin
    ? [
        { to: "/admin/tenants", label: "Tenants" },
        { to: "/admin/plugins", label: "Plugin repositories" },
        { to: "/admin/retrieval", label: "Retrieval models" },
        { to: "/admin/assistant", label: "Assistant" },
      ]
    : [
        { to: "/", label: `${t("entity.workspace")}s`, end: true },
        { to: "/personas", label: "Personas" },
        { to: "/knowledge", label: `${t("entity.knowledge_source")}s` },
        { to: "/process-definitions", label: "Flows" },
        { to: "/schemas", label: "Schemas" },
        { to: "/workflows", label: "Workflows" },
        // Repos is gated the same way every other repo affordance is -- a tenant whose
        // workflow has no repo access shouldn't see a dead section.
        ...(repoAccess ? [{ to: "/repos", label: "Repos" }] : []),
        { to: "/organization", label: "Organization" },
      ];

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border">
        <div className="flex w-full min-w-0 items-center gap-8 overflow-x-auto px-6 py-3">
          <Link
            to={isAdmin ? "/admin/tenants" : "/"}
            className="shrink-0 text-base font-semibold tracking-tight"
          >
            Pyrrhula{isAdmin ? " Admin" : ""}
          </Link>
          <nav className="flex flex-1 items-center gap-1 text-sm">
            {links.map((l) => (
              <NavLink
                key={l.to}
                to={l.to}
                end={l.end}
                className={({ isActive }) =>
                  `rounded-md px-3 py-1.5 transition-colors ${
                    isActive
                      ? "bg-secondary font-medium text-foreground"
                      : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
                  }`
                }
              >
                {l.label}
              </NavLink>
            ))}
          </nav>
          <div className="flex shrink-0 items-center gap-3">
            <MissingVocabularyKeysBadge />
            <UserMenu />
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl px-6 py-6">
        <Outlet />
      </main>
      {!isAdmin && <AssistantWidget />}
    </div>
  );
}
