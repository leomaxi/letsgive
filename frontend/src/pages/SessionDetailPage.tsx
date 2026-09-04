import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { sessionApi } from "@/lib/endpoints";
import { useCountdown } from "@/lib/useCountdown";
import { useOperatorSocket } from "@/lib/useOperatorSocket";
import type { SessionStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Input, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SessionStatus, "slate" | "green" | "amber" | "blue" | "red"> = {
  draft: "slate",
  approval_requested: "amber",
  authorized: "blue",
  live: "green",
  paused: "amber",
  ended: "slate",
  reconciling: "slate",
  closed: "slate",
};

function formatMoney(amount: string | null, currency: string): string {
  if (amount == null) return "—";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(Number(amount));
  } catch {
    return `${currency} ${amount}`;
  }
}

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();
  const role = activeOrg?.role;

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [approvalId, setApprovalId] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string[]>([]);
  const [code, setCode] = useState("");
  const [extendMinutes, setExtendMinutes] = useState(5);
  const [depositAmount, setDepositAmount] = useState("20.00");
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);

  const canOperate = role === "media" || role === "finance";

  const query = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => sessionApi.getOperator(sessionId!),
    enabled: !!sessionId,
    // The WS below pushes updates live; this interval is just a safety net
    // in case that socket silently stalls without firing onclose.
    refetchInterval: 20000,
  });

  const live = useOperatorSocket(sessionId, canOperate);

  const eventsQuery = useQuery({
    queryKey: ["session-events", sessionId],
    queryFn: () => sessionApi.events(sessionId!),
    enabled: !!sessionId,
    refetchInterval: 6000,
  });

  const session = query.data;
  const countdown = useCountdown(session?.ends_at ?? null);

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["sessions", activeOrg?.id] });
    queryClient.invalidateQueries({ queryKey: ["session-events", sessionId] });
  }

  async function runAction<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setError(null);
    setBusy(true);
    try {
      const result = await fn();
      invalidate();
      return result;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  if (!sessionId) return null;

  if (query.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }

  if (!session) {
    return <Card className="text-sm text-slate-500 dark:text-slate-400">Session not found.</Card>;
  }

  const canRequestApproval = role === "media";
  const canToggleVisibility = role === "finance";

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold capitalize text-slate-900 dark:text-slate-100">
            {session.contribution_method.replace("-", " ")} session
          </h1>
          <p className="text-xs text-slate-400 dark:text-slate-500">{session.id}</p>
        </div>
        <div className="flex items-center gap-2">
          {(role === "owner" || role === "finance" || role === "auditor") && (
            <Link
              to={`/sessions/${session.id}/report`}
              className="text-sm text-brand-600 hover:text-brand-700"
            >
              View report
            </Link>
          )}
          {session.test_mode && <Badge tone="amber">test mode</Badge>}
          <Badge tone={STATUS_TONE[session.status]}>{session.status.replace("_", " ")}</Badge>
          {canOperate && (
            <Badge tone={live ? "green" : "slate"}>{live ? "connected" : "reconnecting…"}</Badge>
          )}
        </div>
      </div>

      {session.operator_warning && (
        <div
          role="alert"
          className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300"
        >
          {session.operator_warning}
        </div>
      )}
      <ErrorText>{error}</ErrorText>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
            Contributions
          </div>
          <div className="mt-1 text-3xl font-bold text-slate-900 dark:text-slate-100">{session.contribution_count}</div>
        </Card>
        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">Amount</div>
          <div className="mt-1 text-3xl font-bold text-slate-900 dark:text-slate-100">
            {session.amount_visible ? formatMoney(session.total_amount, session.currency) : "Hidden"}
          </div>
        </Card>
        <Card>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
            Time remaining
          </div>
          <div className="mt-1 text-3xl font-bold text-slate-900 dark:text-slate-100">{countdown ?? "—"}</div>
        </Card>
      </div>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Controls
        </h2>
        <div className="flex flex-wrap items-center gap-3">
          {session.status === "draft" && canRequestApproval && (
            <Button
              disabled={busy}
              onClick={() =>
                runAction(() => sessionApi.requestApproval(session.id)).then((res) => {
                  if (res) {
                    setApprovalId(res.approval_id);
                    setSentTo(res.sent_to);
                  }
                })
              }
            >
              Request finance approval
            </Button>
          )}
          {session.status === "draft" && !canRequestApproval && (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Waiting for a Media teammate to request finance approval.
            </p>
          )}

          {session.status === "approval_requested" && (
            <div className="w-full max-w-sm space-y-2">
              <p className="text-sm text-slate-600 dark:text-slate-300">
                {sentTo.length > 0
                  ? `A code was sent to ${sentTo.join(", ")}. Ask finance to read it to you.`
                  : "A code was sent to your organization's finance officer(s)."}
              </p>
              {canRequestApproval && (
                <div className="flex gap-2">
                  <Input
                    placeholder="6-digit code"
                    inputMode="numeric"
                    maxLength={6}
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                  <Button
                    disabled={busy || code.length !== 6}
                    onClick={async () => {
                      if (!approvalId) {
                        setError("Request approval again in this browser tab first.");
                        return;
                      }
                      await runAction(() => sessionApi.verify(session.id, approvalId, code));
                      setCode("");
                    }}
                  >
                    Verify
                  </Button>
                </div>
              )}
            </div>
          )}

          {session.status === "authorized" && canOperate && (
            <Button disabled={busy} onClick={() => runAction(() => sessionApi.start(session.id, session.version))}>
              Start
            </Button>
          )}

          {session.status === "live" && canOperate && (
            <>
              <Button
                variant="secondary"
                disabled={busy}
                onClick={() => runAction(() => sessionApi.pause(session.id, session.version))}
              >
                Pause
              </Button>
              <div className="flex items-center gap-1">
                <Input
                  type="number"
                  className="w-20"
                  min={1}
                  value={extendMinutes}
                  onChange={(e) => setExtendMinutes(Number(e.target.value))}
                />
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={() =>
                    runAction(() => sessionApi.extend(session.id, session.version, extendMinutes * 60))
                  }
                >
                  Extend (min)
                </Button>
              </div>
              <Button
                variant="danger"
                disabled={busy}
                onClick={() => runAction(() => sessionApi.close(session.id, session.version))}
              >
                Close
              </Button>
            </>
          )}

          {session.status === "paused" && canOperate && (
            <>
              <Button disabled={busy} onClick={() => runAction(() => sessionApi.resume(session.id, session.version))}>
                Resume
              </Button>
              <Button
                variant="danger"
                disabled={busy}
                onClick={() => runAction(() => sessionApi.close(session.id, session.version))}
              >
                Close
              </Button>
            </>
          )}

          {(session.status === "ended" || session.status === "closed") && (
            <p className="text-sm text-slate-500 dark:text-slate-400">This session has ended.</p>
          )}

          {(session.status === "live" || session.status === "paused") && canToggleVisibility && (
            <label className="ml-auto flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
              <input
                type="checkbox"
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
                checked={session.amount_visible}
                disabled={busy}
                onChange={(e) =>
                  runAction(() => sessionApi.setVisibility(session.id, session.version, e.target.checked))
                }
              />
              Show amount publicly
            </label>
          )}
        </div>

        {(session.status === "live" || session.status === "paused") && canOperate && (
          <div className="mt-4 border-t border-slate-100 pt-4 dark:border-slate-700">
            <Button
              variant="secondary"
              disabled={busy}
              onClick={async () => {
                const res = await runAction(() => sessionApi.displayToken(session.id));
                if (res) {
                  const url = `${window.location.protocol}//${window.location.hostname}:8000/display/${session.id}?token=${res.display_token}`;
                  setDisplayUrl(url);
                }
              }}
            >
              Get projection link
            </Button>
            {displayUrl && (
              <div className="mt-2 flex items-center gap-2">
                <a
                  href={displayUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="break-all text-sm text-brand-600 hover:text-brand-700"
                >
                  {displayUrl}
                </a>
              </div>
            )}
          </div>
        )}

        {session.test_mode &&
          (session.status === "live" || session.status === "paused") &&
          canOperate && (
            <div className="mt-4 flex items-center gap-2 border-t border-slate-100 pt-4 dark:border-slate-700">
              <Input
                className="w-32"
                value={depositAmount}
                onChange={(e) => setDepositAmount(e.target.value)}
              />
              <Button
                variant="secondary"
                disabled={busy}
                onClick={() => runAction(() => sessionApi.simulateDeposit(session.id, depositAmount))}
              >
                Simulate deposit
              </Button>
            </div>
          )}
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Ledger events
        </h2>
        {eventsQuery.data?.length ? (
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
            {eventsQuery.data.map((event) => (
              <li key={event.id} className="flex items-center justify-between py-2">
                <div>
                  <div className="text-slate-700 dark:text-slate-300">{new Date(event.received_at).toLocaleString()}</div>
                  {event.decision_reason && (
                    <div className="text-xs text-slate-400 dark:text-slate-500">{event.decision_reason}</div>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  {event.amount && <span className="text-slate-600 dark:text-slate-300">{event.amount}</span>}
                  <Badge tone={event.decision === "accepted" ? "green" : "amber"}>
                    {event.decision.replace(/_/g, " ")}
                  </Badge>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500 dark:text-slate-400">No events yet.</p>
        )}
      </Card>
    </div>
  );
}
