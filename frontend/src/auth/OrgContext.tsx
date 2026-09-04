import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { orgApi } from "@/lib/endpoints";
import type { MyOrganization } from "@/lib/types";
import { useAuth } from "./AuthContext";

const ACTIVE_ORG_STORAGE_KEY = "letsgive.active_org_id";

interface OrgContextValue {
  organizations: MyOrganization[];
  activeOrg: MyOrganization | null;
  isLoading: boolean;
  isFetching: boolean;
  setActiveOrgId: (orgId: string) => void;
  refetch: () => void;
}

const OrgContext = createContext<OrgContextValue | undefined>(undefined);

export function OrgProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [activeOrgId, setActiveOrgIdState] = useState<string | null>(
    () => localStorage.getItem(ACTIVE_ORG_STORAGE_KEY)
  );

  const {
    data: organizations = [],
    isLoading,
    isFetching,
    refetch,
  } = useQuery({
    queryKey: ["my-organizations"],
    queryFn: orgApi.listMine,
    enabled: !!user,
  });

  useEffect(() => {
    if (organizations.length === 0) return;
    const stillValid = organizations.some((o) => o.id === activeOrgId);
    if (!stillValid) {
      setActiveOrgIdState(organizations[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [organizations]);

  function setActiveOrgId(orgId: string) {
    localStorage.setItem(ACTIVE_ORG_STORAGE_KEY, orgId);
    setActiveOrgIdState(orgId);
  }

  const activeOrg = useMemo(
    () => organizations.find((o) => o.id === activeOrgId) ?? null,
    [organizations, activeOrgId]
  );

  return (
    <OrgContext.Provider
      value={{ organizations, activeOrg, isLoading, isFetching, setActiveOrgId, refetch }}
    >
      {children}
    </OrgContext.Provider>
  );
}

export function useOrg(): OrgContextValue {
  const ctx = useContext(OrgContext);
  if (!ctx) throw new Error("useOrg must be used within an OrgProvider");
  return ctx;
}
