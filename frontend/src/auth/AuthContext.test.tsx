import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "@/auth/AuthContext";
import { authApi } from "@/lib/endpoints";
import type { User } from "@/lib/types";

// Regression test for a real cross-account bug: switching accounts in the
// same tab (sign out, sign in as someone else) left every cached query --
// org list, members, sessions, notifications, all keyed with no user id in
// them, since the app only expects one signed-in user per tab -- serving
// the *previous* user's data. The real API calls correctly scoped/404'd for
// the new user, but nothing told already-mounted queries to refetch, so the
// screen silently showed someone else's organization until a hard reload.
// Caught by hand in the browser: logged in as an Owner with a real org,
// signed out, registered and logged in as a brand-new user with zero orgs,
// and watched the previous Owner's org/members list stay on screen.

vi.mock("@/lib/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/endpoints")>();
  return {
    ...actual,
    authApi: { ...actual.authApi, me: vi.fn(), login: vi.fn() },
  };
});

const USER_A: User = { id: "u1", email: "a@example.org", full_name: "A", mfa_enabled: true };
const USER_B: User = { id: "u2", email: "b@example.org", full_name: "B", mfa_enabled: false };

function Probe() {
  const { user, login } = useAuth();
  return (
    <div>
      <span>{user ? `signed in as ${user.email}` : "signed out"}</span>
      <button onClick={() => login("b@example.org", "pw")}>login as b</button>
    </div>
  );
}

it("clears the entire query cache on login, so a new user never sees the previous user's cached data", async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Stand in for whatever the previous user (A) had cached -- an org list,
  // a members list, doesn't matter which key, the fix is a blanket clear().
  queryClient.setQueryData(["my-organizations"], [{ id: "org1", name: "A's Org" }]);

  vi.mocked(authApi.me).mockResolvedValueOnce(USER_A).mockResolvedValueOnce(USER_B);
  vi.mocked(authApi.login).mockResolvedValue({ access_token: "tok-b", token_type: "bearer" });

  localStorage.setItem("letsgive.access_token", "tok-a");

  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>
  );

  await waitFor(() => {
    expect(screen.getByText("signed in as a@example.org")).toBeInTheDocument();
  });
  expect(queryClient.getQueryData(["my-organizations"])).toEqual([{ id: "org1", name: "A's Org" }]);

  await act(async () => {
    screen.getByText("login as b").click();
  });

  await waitFor(() => {
    expect(screen.getByText("signed in as b@example.org")).toBeInTheDocument();
  });
  // The fix: login() clears the cache, so B's screen never renders A's data.
  expect(queryClient.getQueryData(["my-organizations"])).toBeUndefined();
});

describe("logout", () => {
  it("also clears the query cache", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(["my-organizations"], [{ id: "org1", name: "A's Org" }]);
    vi.mocked(authApi.me).mockResolvedValue(USER_A);
    localStorage.setItem("letsgive.access_token", "tok-a");

    function LogoutProbe() {
      const { user, logout } = useAuth();
      return (
        <div>
          <span>{user ? `signed in as ${user.email}` : "signed out"}</span>
          <button onClick={logout}>sign out</button>
        </div>
      );
    }

    render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <LogoutProbe />
        </AuthProvider>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("signed in as a@example.org")).toBeInTheDocument();
    });

    act(() => {
      screen.getByText("sign out").click();
    });

    await waitFor(() => {
      expect(screen.getByText("signed out")).toBeInTheDocument();
    });
    expect(queryClient.getQueryData(["my-organizations"])).toBeUndefined();
  });
});
