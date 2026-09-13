import { Outlet, useParams } from "react-router-dom";
import { BackLink } from "@/components/BackLink";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { AuditIndicator } from "./AuditIndicator";
import { useDirectorViewAccess } from "./useDirectorViewAccess";

/**
 * E2.11: the shared shell for every director-view route. `AuditIndicator` renders here,
 * unconditionally, so no route nested under this layout can ever mount without it. The
 * access check gates the `Outlet` itself -- each individual page's own query is *also*
 * permission-checked server-side and renders its own denial message, so a stale or
 * slow-to-resolve access check here never becomes the only thing standing between a
 * denied principal and real data.
 */
export function DirectorViewLayout() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const t = useLabel();
  const { data: access, isLoading } = useDirectorViewAccess(workspaceId);

  if (!workspaceId) return null;

  return (
    <div className="flex flex-col gap-4">
      <BackLink to={`/workspaces/${workspaceId}`} label="Back to the workspace" />
      <h1 className="text-xl font-semibold">{t("role.overseer")}</h1>
      <AuditIndicator />
      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {access !== undefined && !access.can_inspect && (
        <p className="text-destructive" role="alert">
          You do not have permission to inspect {t("entity.secret")}s in this{" "}
          {t("entity.workspace")}.
        </p>
      )}
      {access?.can_inspect && <Outlet />}
    </div>
  );
}
