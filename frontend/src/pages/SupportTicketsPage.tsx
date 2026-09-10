import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { supportTicketApi } from "@/lib/endpoints";
import type { SupportTicketStatus } from "@/lib/types";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Input, PageHeader, Spinner, StatCard, Textarea } from "@/components/ui";

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
  const tickets = ticketsQuery.data ?? [];
  const openCount = tickets.filter((ticket) => ticket.status === "open" || ticket.status === "in_progress").length;
  const resolvedCount = tickets.filter((ticket) => ticket.status === "resolved" || ticket.status === "closed").length;

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
      <PageHeader
        title="Support"
        description="Open a ticket, track replies, and keep troubleshooting in one place."
      />

      <div className="grid gap-4 md:grid-cols-3">
        <StatCard label="Active tickets" value={openCount} tone={openCount > 0 ? "amber" : "slate"} />
        <StatCard label="Resolved" value={resolvedCount} tone="green" />
        <StatCard label="Total" value={tickets.length} tone="blue" />
      </div>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase text-slate-500 dark:text-slate-400">
          Your tickets
        </h2>
        {ticketsQuery.isLoading ? (
          <Spinner className="h-5 w-5 text-brand-600" />
        ) : tickets.length > 0 ? (
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
            {tickets.map((ticket) => (
              <li key={ticket.id}>
                <Link
                  to={`/support/${ticket.id}`}
                  className="grid gap-3 py-3 hover:text-brand-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 dark:hover:text-brand-400 sm:grid-cols-[1fr_auto]"
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
          <EmptyState title="No tickets yet" description="When something needs attention, open a ticket below and replies will stay attached to this organization." />
        )}
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase text-slate-500 dark:text-slate-400">
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
            <Textarea
              id="body"
              required
              rows={4}
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
