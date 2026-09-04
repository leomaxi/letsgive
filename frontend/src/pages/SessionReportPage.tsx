import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError, getToken } from "@/lib/api";
import { reportApi } from "@/lib/endpoints";
import type { ApiErrorBody } from "@/lib/types";
import { Badge, Button, Card, Spinner } from "@/components/ui";

async function downloadReportFile(
  url: string,
  filename: string,
  formatLabel: string,
  setError: (msg: string | null) => void
) {
  setError(null);
  try {
    const token = getToken();
    const response = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
      let detail = `Export failed (${response.status})`;
      if (response.headers.get("content-type")?.includes("application/json")) {
        const body = (await response.json()) as ApiErrorBody;
        if (typeof body.detail === "string") detail = body.detail;
      }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);
  } catch (err) {
    setError(err instanceof Error ? err.message : `Could not download the ${formatLabel} export.`);
  }
}

export default function SessionReportPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { activeOrg } = useOrg();
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["session-report", sessionId],
    queryFn: () => reportApi.getSessionReport(sessionId!),
    enabled: !!sessionId,
  });

  if (!sessionId || !activeOrg) return null;

  const canView = activeOrg.role === "owner" || activeOrg.role === "finance" || activeOrg.role === "auditor";
  if (!canView) {
    return <Card className="text-sm text-slate-500 dark:text-slate-400">Only Owner, Finance and Auditor can view reports.</Card>;
  }

  if (query.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }

  if (query.isError || !query.data) {
    const message =
      query.error instanceof ApiError ? query.error.message : "Could not load this report.";
    return <Card className="text-sm text-slate-500 dark:text-slate-400">{message}</Card>;
  }

  const report = query.data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link to={`/sessions/${sessionId}`} className="text-sm text-brand-600 hover:text-brand-700">
            ← Back to session
          </Link>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Session report</h1>
        </div>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={() =>
              downloadReportFile(
                reportApi.sessionCsvExportUrl(sessionId),
                `session-${sessionId}.csv`,
                "CSV",
                setDownloadError
              )
            }
          >
            Download CSV
          </Button>
          <Button
            variant="secondary"
            onClick={() =>
              downloadReportFile(
                reportApi.sessionPdfExportUrl(sessionId),
                `session-${sessionId}.pdf`,
                "PDF",
                setDownloadError
              )
            }
          >
            Download PDF
          </Button>
        </div>
      </div>
      {downloadError && <p className="text-sm text-red-600 dark:text-red-400">{downloadError}</p>}

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
            Validated
          </div>
          <div className="mt-1 text-2xl font-bold text-slate-900 dark:text-slate-100">
            {report.validated_count} contribution{report.validated_count === 1 ? "" : "s"}
          </div>
          {report.validated_amount && (
            <div className="text-sm text-slate-500 dark:text-slate-400">{report.validated_amount}</div>
          )}
        </Card>
        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
            Mailbox connection
          </div>
          {report.connection_health ? (
            <div className="mt-1 text-sm text-slate-700 dark:text-slate-300">
              <Badge tone={report.connection_health.status === "connected" ? "green" : "red"}>
                {report.connection_health.status}
              </Badge>
              {report.connection_health.last_sync_at && (
                <div className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                  Last synced {new Date(report.connection_health.last_sync_at).toLocaleString()}
                </div>
              )}
            </div>
          ) : (
            <div className="mt-1 text-sm text-slate-500 dark:text-slate-400">No mailbox connection was bound.</div>
          )}
        </Card>
      </div>

      <Card>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Excluded messages
        </h2>
        {Object.keys(report.excluded_counts).length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">None.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {Object.entries(report.excluded_counts).map(([reason, count]) => (
              <li key={reason} className="flex justify-between">
                <span className="text-slate-600 dark:text-slate-300">{reason.replace(/_/g, " ")}</span>
                <span className="font-medium text-slate-800 dark:text-slate-200">{count}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Corrections
        </h2>
        {report.corrections.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">None.</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {report.corrections.map((c) => (
              <li key={c.id} className="flex justify-between">
                <span className="text-slate-600 dark:text-slate-300">{c.reason}</span>
                <span className="font-medium text-slate-800 dark:text-slate-200">
                  {c.resolution} {c.corrected_amount && `· ${c.corrected_amount}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Approval history
        </h2>
        {report.approval_history.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">None.</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {report.approval_history.map((a) => (
              <li key={a.id} className="flex items-center justify-between">
                <span className="text-slate-600 dark:text-slate-300">{new Date(a.created_at).toLocaleString()}</span>
                <span className="flex items-center gap-2">
                  {a.locked && <Badge tone="red">locked</Badge>}
                  {a.verified_at ? (
                    <Badge tone="green">verified</Badge>
                  ) : (
                    <Badge tone="amber">not verified</Badge>
                  )}
                  <span className="text-xs text-slate-400 dark:text-slate-500">{a.attempts} attempt(s)</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
