import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/lib/queryClient";
import { NotFoundPage } from "@/components/NotFoundPage";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { WorkflowsPage } from "@/features/workflows/WorkflowsPage";
import { OrganizationPage } from "@/features/organization/OrganizationPage";
import { EntitySheetPage } from "@/features/entity-sheets/EntitySheetPage";
import { EntityListPage } from "@/features/entity-sheets/EntityListPage";
import { PersonasEntryPage } from "@/features/agents/PersonasEntryPage";
import { ReposPage } from "@/features/repos/ReposPage";
import { RepoGraphPage } from "@/features/repos/RepoGraphPage";
import { AppShell } from "@/components/AppShell";
import { LoginPage } from "@/features/auth/LoginPage";
import { RegisterPage } from "@/features/auth/RegisterPage";
import { WorkspaceListPage } from "@/features/workspaces/WorkspaceListPage";
import { WorkspaceDetailPage } from "@/features/workspaces/WorkspaceDetailPage";
import { SessionView } from "@/features/session/SessionView";
import { KnowledgeSourceListPage } from "@/features/knowledge/KnowledgeSourceListPage";
import { KnowledgeSourceDetailPage } from "@/features/knowledge/KnowledgeSourceDetailPage";
import { ProcessDefinitionListPage } from "@/features/process-editor/ProcessDefinitionListPage";
import { ProcessEditorPage } from "@/features/process-editor/ProcessEditorPage";
import { SchemaListPage } from "@/features/schema-editor/SchemaListPage";
import { SchemaEditorPage } from "@/features/schema-editor/SchemaEditorPage";
import { AgentManagementPage } from "@/features/agents/AgentManagementPage";
import { SecretListPage } from "@/features/secrets/SecretListPage";
import { ExportDialog } from "@/features/export/ExportDialog";
import { DirectorViewLayout } from "@/features/director-view/DirectorViewLayout";
import { SecretsByHolderPage } from "@/features/director-view/SecretsByHolderPage";
import { DisclosureTimelinePage } from "@/features/director-view/DisclosureTimelinePage";
import { AgentBeliefsPage } from "@/features/director-view/AgentBeliefsPage";
import { AdminTenantsPage } from "@/features/admin/AdminTenantsPage";
import { AdminPluginReposPage } from "@/features/admin/AdminPluginReposPage";
import { AdminModelsPage } from "@/features/admin/AdminModelsPage";
import { AdminAssistantPage } from "@/features/admin/AdminAssistantPage";
import { useMe } from "@/features/admin/useMe";
import { useAdminLanding } from "@/features/admin/useAdminLanding";

/**
 * Where "/" goes.
 *
 * The platform admin's organization is reserved: it has no campaigns to run, so the
 * workspace product at "/" is meaningless there — and landing on it invited an operator
 * into onboarding (create a world, add personas, write hidden motives) that does not
 * apply to them. Login already routes admins to the console; this covers every other way
 * of arriving at the root, which is how they got there.
 */
function HomeRoute() {
  const me = useMe();
  const landing = useAdminLanding();
  if (me.isLoading) return null;
  // `platform_admin` says what you MAY administer; `admin_tenant` says whether you are
  // signed into the reserved admin organization, which is the one with no workspaces of
  // its own to land on. A single-tenant owner is both an ordinary user and an admin --
  // sending them to the console instead of their own workspace would be answering the
  // wrong question.
  if (me.data?.admin_tenant !== true) return <WorkspaceListPage />;
  // Which admin page depends on whether this deployment has a retrieval model yet; see
  // useAdminLanding. Render nothing rather than flashing the wrong page first.
  if (landing.loading) return null;
  return <Navigate to={landing.path} replace />;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />

          <Route element={<ProtectedRoute />}>
            <Route element={<AppShell />}>
              <Route path="/" element={<HomeRoute />} />
              <Route path="/workspaces/:workspaceId" element={<WorkspaceDetailPage />} />
              <Route path="/workspaces/:workspaceId/repo-graph" element={<RepoGraphPage />} />
              <Route path="/workspaces/:workspaceId/agents" element={<AgentManagementPage />} />
              <Route path="/workspaces/:workspaceId/entities" element={<EntityListPage />} />
              <Route path="/workspaces/:workspaceId/entities/:entityId" element={<EntitySheetPage />} />
              <Route path="/workspaces/:workspaceId/secrets" element={<SecretListPage />} />
              <Route path="/workspaces/:workspaceId/export" element={<ExportDialog />} />
              <Route
                path="/workspaces/:workspaceId/director-view"
                element={<DirectorViewLayout />}
              >
                <Route index element={<SecretsByHolderPage />} />
                <Route
                  path="secrets/:secretId/timeline"
                  element={<DisclosureTimelinePage />}
                />
                <Route
                  path="agents/:agentPrincipalId/beliefs"
                  element={<AgentBeliefsPage />}
                />
              </Route>
              <Route path="/sessions/:sessionId" element={<SessionView />} />
              <Route path="/knowledge" element={<KnowledgeSourceListPage />} />
              <Route path="/knowledge/:sourceId" element={<KnowledgeSourceDetailPage />} />
              <Route path="/process-definitions" element={<ProcessDefinitionListPage />} />
              <Route path="/personas" element={<PersonasEntryPage />} />
              <Route path="/workflows" element={<WorkflowsPage />} />
              <Route path="/organization" element={<OrganizationPage />} />
              <Route path="/repos" element={<ReposPage />} />
              <Route path="/process-definitions/new" element={<ProcessEditorPage />} />
              <Route path="/process-definitions/:definitionId" element={<ProcessEditorPage />} />
              <Route path="/schemas" element={<SchemaListPage />} />
              <Route path="/schemas/new" element={<SchemaEditorPage />} />
              <Route path="/schemas/:schemaId" element={<SchemaEditorPage />} />
              <Route path="/admin/tenants" element={<AdminTenantsPage />} />
              <Route path="/admin/plugins" element={<AdminPluginReposPage />} />
              <Route path="/admin/models" element={<AdminModelsPage />} />
              {/* The page was "Retrieval models" before it also held the assistant's
                  connection; keep old links working rather than 404 a bookmark. */}
              <Route
                path="/admin/retrieval"
                element={<Navigate to="/admin/models" replace />}
              />
              <Route path="/admin/assistant" element={<AdminAssistantPage />} />
              {/* In-shell 404 for signed-in users; the bare one below covers signed-out. */}
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Route>

          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
