import { NavLink, Outlet } from "react-router-dom";
import { useMe } from "@/features/admin/useMe";

const TABS: Array<{ to: string; label: string }> = [
  { to: "/admin/models", label: "Models" },
  { to: "/admin/assistant", label: "Assistant" },
  { to: "/admin/plugins", label: "Plugin repositories" },
  { to: "/admin/registries", label: "Registries" },
  { to: "/admin/builders", label: "Builders" },
  { to: "/admin/tenants", label: "Tenants" },
];

/**
 * The admin pages under one "App settings" tab.
 *
 * In the reserved admin organization these pages ARE the top navigation, so the shell
 * already shows them and this layout adds nothing. For a platform admin who is also an
 * ordinary user — the single-tenant owner — the product navigation stays, and the
 * deployment-wide pages sit under this second row instead of replacing everything.
 */
export function AdminSettingsLayout() {
  const me = useMe();
  const adminOrg = me.data?.admin_tenant === true;
  return (
    <div className="flex flex-col gap-4">
      {!adminOrg && (
        <nav className="flex flex-wrap items-center gap-1 border-b border-border pb-2 text-sm">
          <span className="mr-2 text-xs uppercase tracking-wide text-muted-foreground">
            App settings
          </span>
          {TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                `rounded-md px-3 py-1.5 transition-colors ${
                  isActive
                    ? "bg-secondary font-medium text-foreground"
                    : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
                }`
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>
      )}
      <Outlet />
    </div>
  );
}
