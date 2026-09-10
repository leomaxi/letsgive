import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { reconciliationApi, sessionApi } from "@/lib/endpoints";
import type { ReconciliationItem, ReconciliationResolution, ReconciliationStatus } from "@/lib/types";
import { Badge, Button, Card, EmptyState, ErrorText, Input, PageHeader, SegmentedControl, Select, Spinner, StatCard } from "@/components/ui";

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
        <Select
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">Select an event…</option>
          {acceptedEvents.map((e) => (
            <option key={e.id} value={e.id}>
              {e.amount} {e.currency} · {new Date(e.received_at).toLocaleString()}
            </option>
          ))}
        </Select>
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
  const items = query.data ?? [];
  const amountReviewCount = items.filter((item) => item.evidence.amount != null).length;

  if (!activeOrg) return null;

  // Any active member can view -- the backend matches (see
  // app/api/v1/reconciliation.py). Only resolving stays Finance-only.
  const canResolve = activeOrg.role === "finance";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Reconciliation"
        description="Review messages that could not be counted automatically."
        actions={
          <SegmentedControl
            value={statusFilter}
            options={[
              { value: "pending", label: "Pending" },
              { value: "resolved", label: "Resolved" },
            ]}
            onChange={setStatusFilter}
          />
        }
      />

      <div className="grid gap-4 md:grid-cols-3">
        <StatCard
          label={statusFilter === "pending" ? "Waiting review" : "Resolved"}
          value={items.length}
          tone={statusFilter === "pending" && items.length > 0 ? "amber" : "slate"}
        />
        <StatCard label="With amount" value={amountReviewCount} tone="blue" />
        <StatCard
          label="Resolver"
          value={canResolve ? "Finance" : "Read only"}
          detail={canResolve ? "You can accept, exclude, reverse, or mark duplicate." : "Finance can resolve these items."}
          tone={canResolve ? "green" : "slate"}
        />
      </div>

      {query.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          title={statusFilter === "pending" ? "Nothing waiting for review" : "No resolved items yet"}
          description={statusFilter === "pending" ? "Messages that need a decision will appear here." : "Resolved reconciliation decisions will appear here for audit context."}
        />
      ) : (
        <div className="space-y-3">
          {items.map((item) => (
            <Card key={item.id}>
              <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
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
                <div className="flex items-start sm:justify-end">
                  <Badge tone={item.status === "pending" ? "amber" : "slate"}>{item.status}</Badge>
                </div>
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
