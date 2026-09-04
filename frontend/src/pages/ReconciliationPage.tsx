import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { reconciliationApi, sessionApi } from "@/lib/endpoints";
import type { ReconciliationItem, ReconciliationResolution, ReconciliationStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Input, Spinner } from "@/components/ui";

function ReversalPicker({
  sessionId,
  value,
  onChange,
}: {
  sessionId: string;
  value: string;
  onChange: (eventId: string) => void;
}) {
  const eventsQuery = useQuery({
    queryKey: ["session-events", sessionId],
    queryFn: () => sessionApi.events(sessionId),
  });
  const acceptedEvents = (eventsQuery.data ?? []).filter((e) => e.decision === "accepted");

  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
        Which accepted contribution does this reverse?
      </label>
      {eventsQuery.isLoading ? (
        <Spinner className="h-4 w-4 text-brand-600" />
      ) : acceptedEvents.length === 0 ? (
        <p className="text-xs text-slate-400 dark:text-slate-500">No accepted contributions in this session yet.</p>
      ) : (
        <select
          className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">Select an event…</option>
          {acceptedEvents.map((e) => (
            <option key={e.id} value={e.id}>
              {e.amount} {e.currency} · {new Date(e.received_at).toLocaleString()}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

function ResolveForm({ item, canResolve }: { item: ReconciliationItem; canResolve: boolean }) {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();
  const [correctedAmount, setCorrectedAmount] = useState(
    typeof item.evidence.amount === "string" ? item.evidence.amount : ""
  );
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<ReconciliationResolution | null>(null);
  const [reversesEventId, setReversesEventId] = useState("");

  const resolveMutation = useMutation({
    mutationFn: (resolution: ReconciliationResolution) =>
      reconciliationApi.resolve(item.id, resolution, correctedAmount, note, reversesEventId),
    onSuccess: () => {
      setError(null);
      setPendingAction(null);
      queryClient.invalidateQueries({ queryKey: ["reconciliation", activeOrg?.id] });
      queryClient.invalidateQueries({ queryKey: ["session", item.session_id] });
      queryClient.invalidateQueries({ queryKey: ["session-events", item.session_id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not resolve this item."),
  });

  if (!canResolve) {
    return <p className="text-xs text-slate-400 dark:text-slate-500">Only Finance can resolve reconciliation items.</p>;
  }

  function startAction(resolution: ReconciliationResolution) {
    setError(null);
    if (resolution === "reversed") {
      setPendingAction("reversed");
      return;
    }
    resolveMutation.mutate(resolution);
  }

  return (
    <div className="mt-2 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-32"
          placeholder="Amount"
          value={correctedAmount}
          onChange={(e) => setCorrectedAmount(e.target.value)}
        />
        <Input
          className="flex-1"
          placeholder="Note (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </div>

      {pendingAction === "reversed" ? (
        <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-900">
          <ReversalPicker
            sessionId={item.session_id}
            value={reversesEventId}
            onChange={setReversesEventId}
          />
          <div className="flex gap-2">
            <Button
              disabled={resolveMutation.isPending || !reversesEventId}
              onClick={() => resolveMutation.mutate("reversed")}
            >
              {resolveMutation.isPending ? "Confirming…" : "Confirm reversal"}
            </Button>
            <Button
              variant="secondary"
              disabled={resolveMutation.isPending}
              onClick={() => setPendingAction(null)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button disabled={resolveMutation.isPending} onClick={() => startAction("accepted")}>
            Accept
          </Button>
          <Button
            variant="secondary"
            disabled={resolveMutation.isPending}
            onClick={() => startAction("excluded")}
          >
            Exclude
          </Button>
          <Button
            variant="secondary"
            disabled={resolveMutation.isPending}
            onClick={() => startAction("reversed")}
          >
            Reversed
          </Button>
          <Button
            variant="secondary"
            disabled={resolveMutation.isPending}
            onClick={() => startAction("duplicate")}
          >
            Duplicate
          </Button>
        </div>
      )}
      <ErrorText>{error}</ErrorText>
    </div>
  );
}

export default function ReconciliationPage() {
  const { activeOrg } = useOrg();
  const [statusFilter, setStatusFilter] = useState<ReconciliationStatus>("pending");

  const query = useQuery({
    queryKey: ["reconciliation", activeOrg?.id, statusFilter],
    queryFn: () => reconciliationApi.listForOrg(activeOrg!.id, statusFilter),
    enabled: !!activeOrg,
  });

  if (!activeOrg) return null;

  const canView = activeOrg.role === "owner" || activeOrg.role === "finance" || activeOrg.role === "auditor";
  if (!canView) {
    return (
      <Card className="text-sm text-slate-500 dark:text-slate-400">
        Only Owner, Finance and Auditor can view the reconciliation queue.
      </Card>
    );
  }

  const canResolve = activeOrg.role === "finance";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Reconciliation</h1>
        <div className="flex gap-1 rounded-md border border-slate-200 bg-white p-1 text-sm dark:border-slate-700 dark:bg-slate-800">
          {(["pending", "resolved"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`rounded px-3 py-1 ${
                statusFilter === s
                  ? "bg-brand-600 text-white"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {query.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : query.data?.length === 0 ? (
        <Card className="text-sm text-slate-500 dark:text-slate-400">
          {statusFilter === "pending" ? "Nothing waiting for review." : "No resolved items yet."}
        </Card>
      ) : (
        <div className="space-y-3">
          {query.data?.map((item) => (
            <Card key={item.id}>
              <div className="flex items-start justify-between">
                <div>
                  <p className="text-sm font-medium text-slate-800 dark:text-slate-200">{item.reason}</p>
                  <Link
                    to={`/sessions/${item.session_id}`}
                    className="text-xs text-brand-600 hover:text-brand-700"
                  >
                    View session
                  </Link>
                  {item.evidence.amount != null && (
                    <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                      Detected amount: {String(item.evidence.amount)} {String(item.evidence.currency ?? "")}
                      {item.evidence.parser_confidence != null &&
                        ` · confidence ${Number(item.evidence.parser_confidence).toFixed(2)}`}
                    </p>
                  )}
                </div>
                <Badge tone={item.status === "pending" ? "amber" : "slate"}>{item.status}</Badge>
              </div>

              {item.status === "pending" ? (
                <ResolveForm item={item} canResolve={canResolve} />
              ) : (
                <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                  Resolved as <span className="font-medium">{item.resolution}</span>
                  {item.corrected_amount && ` · ${item.corrected_amount}`}
                  {item.resolution_note && ` · "${item.resolution_note}"`}
                </p>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
