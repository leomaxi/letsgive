import { useQuery } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { auditApi } from "@/lib/endpoints";
import { Card, Spinner } from "@/components/ui";

export default function AuditLogPage() {
  const { activeOrg } = useOrg();

  const query = useQuery({
    queryKey: ["audit-log", activeOrg?.id],
    queryFn: () => auditApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  if (!activeOrg) return null;

  const canView = activeOrg.role === "owner" || activeOrg.role === "finance" || activeOrg.role === "auditor";
  if (!canView) {
    return <Card className="text-sm text-slate-500 dark:text-slate-400">Only Owner, Finance and Auditor can view the audit log.</Card>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Audit log</h1>

      {query.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : (
        <Card className="p-0">
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
            {query.data?.map((entry) => (
              <li key={entry.id} className="flex items-center justify-between px-6 py-3">
                <div>
                  <div className="font-medium text-slate-700 dark:text-slate-300">{entry.action}</div>
                  <div className="text-xs text-slate-400 dark:text-slate-500">
                    {entry.target_type}
                    {entry.target_id && ` · ${entry.target_id}`}
                  </div>
                </div>
                <div className="text-right text-xs text-slate-400 dark:text-slate-500">
                  <div>{new Date(entry.created_at).toLocaleString()}</div>
                  {entry.ip_address && <div>{entry.ip_address}</div>}
                </div>
              </li>
            ))}
          </ul>
          {query.data?.length === 0 && (
            <p className="px-6 py-4 text-sm text-slate-500 dark:text-slate-400">No audit events yet.</p>
          )}
        </Card>
      )}
    </div>
  );
}
