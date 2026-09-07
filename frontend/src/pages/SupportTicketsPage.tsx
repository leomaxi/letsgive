import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { supportTicketApi } from "@/lib/endpoints";
import type { SupportTicketStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Input, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SupportTicketStatus, "amber" | "blue" | "green" | "slate"> = {
  open: "amber",
  in_progress: "blue",
  resolved: "green",
  closed: "slate",
};

export default function SupportTicketsPage() {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);

  const ticketsQuery = useQuery({
    queryKey: ["support-tickets", activeOrg?.id],
    queryFn: () => supportTicketApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const createMutation = useMutation({
    mutationFn: () => supportTicketApi.create(activeOrg!.id, subject, body),
    onSuccess: () => {
      setSubject("");
      setBody("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["support-tickets", activeOrg?.id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not create the ticket."),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    createMutation.mutate();
  }

  if (!activeOrg) return null;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Support</h1>
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Need help troubleshooting something? Open a ticket and our team will follow up here.
      </p>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Your tickets
        </h2>
        {ticketsQuery.isLoading ? (
          <Spinner className="h-5 w-5 text-brand-600" />
        ) : ticketsQuery.data && ticketsQuery.data.length > 0 ? (
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
            {ticketsQuery.data.map((ticket) => (
              <li key={ticket.id}>
                <Link
                  to={`/support/${ticket.id}`}
                  className="flex items-center justify-between py-3 hover:text-brand-700 dark:hover:text-brand-400"
                >
                  <div>
                    <div className="font-medium text-slate-700 dark:text-slate-300">{ticket.subject}</div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">
                      updated {new Date(ticket.updated_at).toLocaleString()}
                    </div>
                  </div>
                  <Badge tone={STATUS_TONE[ticket.status]}>{ticket.status.replace("_", " ")}</Badge>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-500 dark:text-slate-400">No tickets yet.</p>
        )}
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Open a new ticket
        </h2>
        <form onSubmit={onSubmit} className="space-y-3" noValidate>
          <Field label="Subject" htmlFor="subject">
            <Input
              id="subject"
              required
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="What's going on?"
            />
          </Field>
          <Field label="Describe the issue" htmlFor="body">
            <textarea
              id="body"
              required
              rows={4}
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="The more detail, the faster we can help."
            />
          </Field>
          <ErrorText>{error}</ErrorText>
          <Button type="submit" disabled={createMutation.isPending}>
            {createMutation.isPending ? "Submitting…" : "Submit ticket"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
