import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LoginPage } from "../LoginPage";
import { RegisterPage } from "../RegisterPage";

/**
 * Single-tenant mode is only real if the form stops asking.
 *
 * The API half shipped first: a header-less login resolves the deployment's one
 * organization. The form went on rendering an "Organization" field anyway, because
 * nothing told it the deployment's shape — which meant the promise ("a solo user never
 * thinks about organizations") was kept by the server and broken by the screen. These
 * assert the screen, not the bundle.
 */

const configs = {
  single: { single_tenant: true, allow_signup: true, has_organization: true },
  singleFirstRun: { single_tenant: true, allow_signup: true, has_organization: false },
  multi: { single_tenant: false, allow_signup: true, has_organization: true },
};

let currentConfig = configs.single;

vi.mock("@/lib/api-client/client", () => ({
  apiClient: {
    GET: vi.fn(async () => ({ data: currentConfig, error: undefined })),
    POST: vi.fn(async () => ({ data: undefined, error: { detail: "nope" } })),
  },
}));

function renderPage(node: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LoginPage tenancy", () => {
  beforeEach(() => {
    currentConfig = configs.single;
  });

  it("test_single_tenant_login_has_no_organization_field", async () => {
    currentConfig = configs.single;
    renderPage(<LoginPage />);

    await waitFor(() => expect(screen.getByLabelText(/email/i)).toBeInTheDocument());
    expect(screen.queryByLabelText(/organization/i)).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("acme-robotics")).not.toBeInTheDocument();
  });

  it("test_multi_tenant_login_still_asks_for_the_organization", async () => {
    currentConfig = configs.multi;
    renderPage(<LoginPage />);

    await waitFor(() => expect(screen.getByLabelText(/organization/i)).toBeInTheDocument());
  });
});

describe("RegisterPage tenancy", () => {
  it("test_solo_deployment_with_an_org_offers_joining_it_not_making_another", async () => {
    // Creating a *second* organization is the one action that breaks single-tenant
    // inference, and the old page led with it as the default tab.
    currentConfig = configs.single;
    renderPage(<RegisterPage />);

    await waitFor(() => expect(screen.getByLabelText(/display name/i)).toBeInTheDocument());
    expect(screen.queryByText("New organization")).not.toBeInTheDocument();
    expect(screen.queryByText("Join existing")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/organization/i)).not.toBeInTheDocument();
    expect(screen.getByText(/create your account/i)).toBeInTheDocument();
  });

  it("test_solo_deployment_with_no_org_yet_asks_for_one", async () => {
    // The first signup on a fresh box IS the organization; this is the one time a solo
    // deployment should ask for its name.
    currentConfig = configs.singleFirstRun;
    renderPage(<RegisterPage />);

    await waitFor(() =>
      expect(screen.getByLabelText(/organization name/i)).toBeInTheDocument(),
    );
  });

  it("test_multi_tenant_register_keeps_both_ways_in", async () => {
    currentConfig = configs.multi;
    renderPage(<RegisterPage />);

    await waitFor(() => expect(screen.getByText("New organization")).toBeInTheDocument());
    expect(screen.getByText("Join existing")).toBeInTheDocument();
  });
});
