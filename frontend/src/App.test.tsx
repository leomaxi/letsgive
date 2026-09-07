import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HomeRoute } from "@/App";
import RequireAuth from "@/auth/RequireAuth";
import { AuthProvider } from "@/auth/AuthContext";
import { OrgProvider } from "@/auth/OrgContext";
import { authApi, invitationApi, joinRequestApi, orgApi } from "@/lib/endpoints";
import type { Invitation, MyOrganization, User } from "@/lib/types";

vi.mock("@/lib/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/endpoints")>();
  return {
    ...actual,
    authApi: { ...actual.authApi, me: vi.fn() },
    orgApi: { ...actual.orgApi, listMine: vi.fn() },
    invitationApi: { ...actual.invitationApi, list: vi.fn() },
    joinRequestApi: { ...actual.joinRequestApi, listMine: vi.fn() },
  };
});

// OrgHomePage pulls in its own member-list query, unrelated to what these
// tests exercise (HomeRoute's own branching) -- stub it so a test only
// needs to mock the endpoints HomeRoute itself actually depends on.
vi.mock("@/pages/OrgHomePage", () => ({
  default: () => <div>ORG HOME STUB</div>,
}));

const USER: User = {
  id: "u1",
  email: "a@example.org",
  full_name: "A",
  mfa_enabled: true,
  is_platform_admin: false,
};

function makeOrg(overrides: Partial<MyOrganization> = {}): MyOrganization {
  return {
    id: "org1",
    name: "Org One",
    legal_name: null,
    country: "CA",
    timezone: "UTC",
    currency: "CAD",
    nonprofit_type: null,
    join_code: "AB2CD3EF",
    status: "active",
    plan_id: "plan1",
    subscription_status: "trialing",
    grace_period_ends_at: null,
    role: "owner",
    membership_status: "active",
    ...overrides,
  };
}

function makeInvitation(overrides: Partial<Invitation> = {}): Invitation {
  return {
    id: "inv1",
    organization_id: "org1",
    organization_name: "Org One",
    role: "media",
    status: "invited",
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function renderHome() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  localStorage.setItem("letsgive.access_token", "test-token");
  vi.mocked(authApi.me).mockResolvedValue(USER);

  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/"]}>
        <AuthProvider>
          <OrgProvider>
            <Routes>
              <Route element={<RequireAuth />}>
                <Route path="/" element={<HomeRoute />} />
                <Route path="/organizations/new" element={<div>CREATE ORG PAGE</div>} />
              </Route>
            </Routes>
          </OrgProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );

  return { ...view, queryClient };
}

describe("HomeRoute", () => {
  beforeEach(() => {
    // No test in this file exercises join requests specifically -- default
    // to none so HomeRoute's extra query doesn't need mocking in every case.
    vi.mocked(joinRequestApi.listMine).mockResolvedValue([]);
  });

  it("redirects to /organizations/new once both org and invitation lists settle empty", async () => {
    vi.mocked(orgApi.listMine).mockResolvedValue([]);
    vi.mocked(invitationApi.list).mockResolvedValue([]);

    renderHome();

    await waitFor(() => {
      expect(screen.getByText("CREATE ORG PAGE")).toBeInTheDocument();
    });
  });

  it("shows the org home once organizations resolve, without ever redirecting", async () => {
    vi.mocked(orgApi.listMine).mockResolvedValue([makeOrg()]);
    vi.mocked(invitationApi.list).mockResolvedValue([]);

    renderHome();

    await waitFor(() => {
      expect(screen.getByText("ORG HOME STUB")).toBeInTheDocument();
    });
    expect(screen.queryByText("CREATE ORG PAGE")).not.toBeInTheDocument();
  });

  it("shows pending invitations instead of redirecting when there are no orgs yet", async () => {
    vi.mocked(orgApi.listMine).mockResolvedValue([]);
    vi.mocked(invitationApi.list).mockResolvedValue([makeInvitation()]);

    renderHome();

    await waitFor(() => {
      expect(screen.getByText("Org One")).toBeInTheDocument();
    });
    expect(screen.queryByText("CREATE ORG PAGE")).not.toBeInTheDocument();
  });

  // Regression test for a real bug. It's specifically a *refetch* race, not
  // an initial-load one: accepting an invitation invalidates both the org
  // list and the invitations list, and the two refetches can land a render
  // apart. During a refetch of already-cached data, react-query's
  // `isLoading` is false throughout (only `isFetching` flips) -- so a
  // version of the redirect check that didn't also wait on `isFetching`
  // would see one stale frame where invitations had already resolved to
  // empty but the org list's refetch (about to turn up the newly-accepted
  // org) hadn't landed yet, read that as "no orgs, no invites", and bounce
  // the user to /organizations/new right after they accepted something.
  it("does not bounce to /organizations/new mid-refetch when invitations resolve empty before the org list's refetch lands", async () => {
    // Phase-based (not call-count-based) mocks: HomeRoute and
    // InvitationsCard each mount their own observer on ["my-invitations"],
    // so the number of underlying calls to invitationApi.list isn't
    // reliable to key off of -- what matters is "what would the API return
    // right now", which these phases model directly.
    let orgsPhase: "empty" | "pending" | "resolved" = "empty";
    const orgsRefetch = deferred<MyOrganization[]>();
    vi.mocked(orgApi.listMine).mockImplementation(() => {
      if (orgsPhase === "empty") return Promise.resolve([]);
      if (orgsPhase === "pending") return orgsRefetch.promise;
      return Promise.resolve([makeOrg()]);
    });

    let invitesPhase: "pending" | "resolved" = "pending";
    vi.mocked(invitationApi.list).mockImplementation(() =>
      Promise.resolve(invitesPhase === "pending" ? [makeInvitation()] : [])
    );

    const { queryClient } = renderHome();

    // Initial settle: has an invitation, no orgs yet -- shows the invite,
    // not the redirect.
    await waitFor(() => {
      expect(screen.getByText("Org One")).toBeInTheDocument();
    });
    expect(screen.queryByText("CREATE ORG PAGE")).not.toBeInTheDocument();

    // The org list's refetch starts first and is held pending on
    // orgsRefetch (not a fleeting race -- exact microtask interleaving
    // between simultaneous invalidateQueries calls isn't something a test
    // should depend on). Confirmed in flight before moving on.
    orgsPhase = "pending";
    act(() => {
      void queryClient.invalidateQueries({ queryKey: ["my-organizations"] });
    });
    await waitFor(() => {
      expect(queryClient.getQueryState(["my-organizations"])?.fetchStatus).toBe("fetching");
    });

    // *Then*, while that's still in flight, invitations settles to empty.
    // At this instant: invitations.isFetching is false (already resolved)
    // and organizations.isFetching is true -- exactly the state a guard
    // that only checked "0 orgs, 0 invites" without also checking
    // isFetching would misread as "nothing at all, redirect".
    invitesPhase = "resolved";
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["my-invitations"] });
    });
    await waitFor(() => {
      expect(screen.queryByText("Org One")).not.toBeInTheDocument();
    });
    expect(screen.queryByText("CREATE ORG PAGE")).not.toBeInTheDocument();

    // Now the org list's refetch lands with the newly-accepted org.
    orgsPhase = "resolved";
    await act(async () => {
      orgsRefetch.resolve([makeOrg()]);
      await orgsRefetch.promise;
    });

    await waitFor(() => {
      expect(screen.getByText("ORG HOME STUB")).toBeInTheDocument();
    });
    expect(screen.queryByText("CREATE ORG PAGE")).not.toBeInTheDocument();
  });
});
