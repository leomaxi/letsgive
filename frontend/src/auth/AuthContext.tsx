import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { getToken, setToken } from "@/lib/api";
import { authApi } from "@/lib/endpoints";
import type { User } from "@/lib/types";

interface AuthContextValue {
  user: User | null;
  isLoading: boolean;
  login: (email: string, password: string, mfaCode?: string) => Promise<void>;
  register: (email: string, password: string, fullName: string) => Promise<void>;
  logout: () => void;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  async function refreshUser() {
    if (!getToken()) {
      setUser(null);
      return;
    }
    try {
      const me = await authApi.me();
      setUser(me);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setToken(null);
        setUser(null);
      } else {
        throw err;
      }
    }
  }

  useEffect(() => {
    refreshUser().finally(() => setIsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function login(email: string, password: string, mfaCode?: string) {
    const { access_token } = await authApi.login(email, password, mfaCode);
    // Every query in the cache (org list, sessions, members, notifications,
    // ...) is keyed with no user id in it, since the app only ever expects
    // one signed-in user per tab. Switching accounts in the same tab without
    // clearing it would otherwise keep serving the *previous* user's cached
    // data -- the real API calls correctly 404/scope to the new user, but
    // nothing tells already-mounted queries to refetch, so the screen would
    // silently show someone else's organization until a hard reload.
    queryClient.clear();
    setToken(access_token);
    await refreshUser();
  }

  async function register(email: string, password: string, fullName: string) {
    await authApi.register(email, password, fullName);
  }

  function logout() {
    queryClient.clear();
    setToken(null);
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, isLoading, login, register, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
