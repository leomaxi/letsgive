import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { sessionApi } from "@/lib/endpoints";
import type { SessionStatus } from "@/lib/types";
import { Badge, Button, Card, Spinner } from "@/components/ui";

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

export default function SessionsListPage() {
  const { activeOrg } = useOrg();

  const sessionsQuery = useQuery({
    queryKey: ["sessions", activeOrg?.id],
    queryFn: () => sessionApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
    refetchInterval: 5000,
  });

  if (!activeOrg) return null;

  const canCreate = activeOrg.role === "media" || activeOrg.role === "finance";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Sessions</h1>
        {canCreate && (
          <Link to="/sessions/new">
            <Button>New session</Button>
          </Link>
        )}
      </div>

      {sessionsQuery.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : sessionsQuery.data?.length === 0 ? (
        <Card className="text-center text-sm text-slate-500 dark:text-slate-400">
          No sessions yet.{" "}
          {canCreate ? (
            <>
              <Link to="/sessions/new" className="font-medium text-brand-600 hover:text-brand-700">
                Create one
              </Link>{" "}
              to get started.
            </>
          ) : (
            "A Media or Finance teammate can create one."
          )}
        </Card>
      ) : (
        <Card className="p-0">
          <ul className="divide-y divide-slate-100 dark:divide-slate-700">
            {sessionsQuery.data?.map((session) => (
              <li key={session.id}>
                <Link
                  to={`/sessions/${session.id}`}
                  className="flex items-center justify-between px-6 py-4 hover:bg-slate-50 dark:hover:bg-slate-700"
                >
                  <div>
                    <div className="font-medium text-slate-800 dark:text-slate-200">{session.contribution_method}</div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">
                      {session.contribution_count} contribution
                      {session.contribution_count === 1 ? "" : "s"}
                      {session.test_mode && " · test mode"}
                    </div>
                  </div>
                  <Badge tone={STATUS_TONE[session.status]}>{session.status.replace("_", " ")}</Badge>
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
