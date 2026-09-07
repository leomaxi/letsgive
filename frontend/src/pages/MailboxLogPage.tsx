import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { connectionApi } from "@/lib/endpoints";
import { Badge, Card, Spinner } from "@/components/ui";

const DECISION_TONE: Record<string, "slate" | "green" | "red" | "amber"> = {
  accepted: "green",
  ambiguous: "amber",
  excluded_time_window: "amber",
  excluded_source_mismatch: "red",
  excluded_no_credit_intent: "red",
  excluded_no_active_session: "red",
  reversed: "slate",
};

function ExpandableSnippet({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = text.length > 200;
  return (
    <div>
      <pre className="whitespace-pre-wrap break-words font-sans text-xs text-slate-600 dark:text-slate-300">
        {expanded || !isLong ? text : `${text.slice(0, 200)}…`}
      </pre>
      {isLong && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-xs text-brand-600 hover:text-brand-700"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

export default function MailboxLogPage() {
  const { activeOrg } = useOrg();
  const { connectionId } = useParams<{ connectionId: string }>();

  const query = useQuery({
    queryKey: ["recent-imap-messages", activeOrg?.id, connectionId],
    queryFn: () => connectionApi.recentMessages(activeOrg!.id, connectionId!),
    enabled: !!activeOrg && !!connectionId,
    refetchInterval: 15000,
  });

  if (!activeOrg) return null;

  if (activeOrg.role !== "owner" && activeOrg.role !== "finance") {
    return (
      <Card className="text-sm text-slate-500 dark:text-slate-400">
        Only Owners and Finance officers can view the mailbox log.
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to="/mailbox" className="text-sm text-brand-600 hover:text-brand-700">
          ← Back to Mailbox &amp; parsing
        </Link>
        <h1 className="mt-2 text-2xl font-semibold text-slate-900 dark:text-slate-100">Mailbox log</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Every message the app has fetched from this mailbox recently, exactly as received —
          before the parser's decision is applied to anything else in the app. Use this to see
          whether a real deposit notification even arrived and what the parser made of it. This
          list isn't saved anywhere; it resets if the server restarts, and only shows the last 25
          messages.
        </p>
      </div>

      {query.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : query.data && query.data.length > 0 ? (
        <ul className="space-y-3">
          {query.data.map((m) => (
            <li key={`${m.uid}-${m.fetched_at}`}>
              <Card>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-medium text-slate-800 dark:text-slate-200">{m.subject || "(no subject)"}</div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">
                      From {m.sender} · received {new Date(m.received_at).toLocaleString()} · fetched{" "}
                      {new Date(m.fetched_at).toLocaleString()}
                    </div>
                  </div>
                  <Badge tone={DECISION_TONE[m.decision] ?? "slate"}>{m.decision.replace(/_/g, " ")}</Badge>
                </div>
                {m.decision_reason && (
                  <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">{m.decision_reason}</p>
                )}
                <div className="mt-3 rounded-md bg-slate-50 p-3 dark:bg-slate-900">
                  <ExpandableSnippet text={m.body_snippet} />
                </div>
              </Card>
            </li>
          ))}
        </ul>
      ) : (
        <Card className="text-sm text-slate-500 dark:text-slate-400">
          Nothing fetched yet. Click "Check now" on this connection in Mailbox &amp; parsing, then
          come back here.
        </Card>
      )}
    </div>
  );
}
