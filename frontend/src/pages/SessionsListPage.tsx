import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { sessionApi } from "@/lib/endpoints";
import type { SessionStatus } from "@/lib/types";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Select, Spinner, StatCard } from "@/components/ui";

const STATUS_TONE: Record<SessionStatus, "slate" | "green" | "amber" | "blue"> = {
  draft: "slate",
  approval_requested: "amber",
  authorized: "blue",
  live: "green",
  paused: "amber",
  ended: "slate",
  reconciling: "slate",
  closed: "slate",
};

const STATUS_FILTERS: Array<"all" | SessionStatus> = [
  "all",
  "live",
  "authorized",
  "approval_requested",
  "draft",
  "paused",
  "ended",
  "closed",
];

function statusLabel(status: string): string {
  return status.replace(/_/g, " ");
}

export default function SessionsListPage() {
  const { activeOrg } = useOrg();
  const [statusFilter, setStatusFilter] = useState<"all" | SessionStatus>("all");
  const [search, setSearch] = useState("");

  const sessionsQuery = useQuery({
    queryKey: ["sessions", activeOrg?.id],
    queryFn: () => sessionApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
    refetchInterval: 5000,
  });

  const sessions = useMemo(() => sessionsQuery.data ?? [], [sessionsQuery.data]);
  const filteredSessions = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return sessions.filter((session) => {
      if (statusFilter !== "all" && session.status !== statusFilter) return false;
      if (!needle) return true;
      return (
        session.contribution_method.toLowerCase().includes(needle) ||
        session.status.toLowerCase().includes(needle) ||
        session.id.toLowerCase().includes(needle)
      );
    });
  }, [search, sessions, statusFilter]);

  const liveCount = sessions.filter((session) => session.status === "live").length;
  const pendingCount = sessions.filter((session) =>
    ["draft", "approval_requested", "authorized"].includes(session.status)
  ).length;
  const totalContributions = sessions.reduce((sum, session) => sum + session.contribution_count, 0);

  if (!activeOrg) return null;

  const canCreate =
    activeOrg.role === "owner" || activeOrg.role === "media" || activeOrg.role === "finance";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Sessions"
        description="Create, monitor, and review giving sessions for the active organization."
        actions={
          canCreate && (
          <Link to="/sessions/new">
            <Button>New session</Button>
          </Link>
          )
        }
      />

      <div className="grid gap-4 md:grid-cols-3">
        <StatCard label="Live now" value={liveCount} tone={liveCount > 0 ? "green" : "slate"} />
        <StatCard label="Needs attention" value={pendingCount} tone={pendingCount > 0 ? "amber" : "slate"} />
        <StatCard label="Total contributions" value={totalContributions} tone="blue" />
      </div>

      {sessionsQuery.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : sessions.length === 0 ? (
        <EmptyState
          title="No sessions yet"
          description={canCreate ? "Create the first session when you are ready to rehearse or go live." : "A Media or Finance teammate can create the first session."}
          action={
            canCreate ? (
              <Link to="/sessions/new">
                <Button>New session</Button>
              </Link>
            ) : null
          }
        />
      ) : (
        <Card className="p-0">
          <div className="grid gap-3 border-b border-slate-100 p-4 dark:border-slate-700 sm:grid-cols-[1fr_12rem]">
            <Input
              placeholder="Search sessions"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value as "all" | SessionStatus)}>
              {STATUS_FILTERS.map((status) => (
                <option key={status} value={status}>
                  {status === "all" ? "All statuses" : statusLabel(status)}
                </option>
              ))}
            </Select>
          </div>
          {filteredSessions.length === 0 ? (
            <div className="px-6 py-10 text-center text-sm text-slate-500 dark:text-slate-400">
              No sessions match the current filters.
            </div>
          ) : (
          <ul className="divide-y divide-slate-100 dark:divide-slate-700">
            {filteredSessions.map((session) => (
              <li key={session.id}>
                <Link
                  to={`/sessions/${session.id}`}
                  className="grid gap-3 px-6 py-4 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-500 dark:hover:bg-slate-700 sm:grid-cols-[1fr_auto]"
                >
                  <div>
                    <div className="font-medium capitalize text-slate-800 dark:text-slate-200">
                      {session.contribution_method.replace("-", " ")}
                    </div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">
                      {session.contribution_count} contribution
                      {session.contribution_count === 1 ? "" : "s"}
                      {session.starts_at && ` · ${new Date(session.starts_at).toLocaleString()}`}
                      {session.test_mode && " · test mode"}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 sm:justify-end">
                    {session.mailbox_connection_id ? (
                      <span className="text-xs text-slate-500 dark:text-slate-400">Mailbox linked</span>
                    ) : (
                      <span className="text-xs text-amber-600 dark:text-amber-400">No mailbox</span>
                    )}
                    <Badge tone={STATUS_TONE[session.status]}>{statusLabel(session.status)}</Badge>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
          )}
        </Card>
      )}
    </div>
  );
}
