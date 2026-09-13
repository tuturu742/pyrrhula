import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { DirectorViewLayout } from "../DirectorViewLayout";
import { SecretsByHolderPage } from "../SecretsByHolderPage";
import { DisclosureTimelinePage } from "../DisclosureTimelinePage";
import { AgentBeliefsPage } from "../AgentBeliefsPage";

function withProviders(children: ReactNode, initialPath: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

/** Renders just the given page, routed as its own director-view path -- without the
 * layout, so a page's own permission-denial handling can be exercised in isolation from
 * the layout's own access gate. */
export function renderDirectorViewPage(initialPath: string) {
  return render(
    withProviders(
      <Routes>
        <Route path="/workspaces/:workspaceId/director-view" element={<SecretsByHolderPage />} />
        <Route
          path="/workspaces/:workspaceId/director-view/secrets/:secretId/timeline"
          element={<DisclosureTimelinePage />}
        />
        <Route
          path="/workspaces/:workspaceId/director-view/agents/:agentPrincipalId/beliefs"
          element={<AgentBeliefsPage />}
        />
      </Routes>,
      initialPath,
    ),
  );
}

/** Renders the real route tree, `DirectorViewLayout` included -- for anything that needs
 * to prove a property of the *layout* (the persistent audit indicator, the access gate),
 * not just a single page in isolation. */
export function renderDirectorViewRoute(initialPath: string) {
  return render(
    withProviders(
      <Routes>
        <Route path="/workspaces/:workspaceId/director-view" element={<DirectorViewLayout />}>
          <Route index element={<SecretsByHolderPage />} />
          <Route path="secrets/:secretId/timeline" element={<DisclosureTimelinePage />} />
          <Route path="agents/:agentPrincipalId/beliefs" element={<AgentBeliefsPage />} />
        </Route>
      </Routes>,
      initialPath,
    ),
  );
}
