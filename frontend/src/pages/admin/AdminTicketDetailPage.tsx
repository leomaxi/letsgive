import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { adminApi } from "@/lib/endpoints";
import type { SupportTicketStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SupportTicketStatus, "amber" | "blue" | "green" | "slate"> = {
  open: "amber",
  in_progress: "blue",
  resolved: "green",
  closed: "slate",
};

const STATUS_OPTIONS: SupportTicketStatus[] = ["open", "in_progress", "resolved", "closed"];

export default function AdminTicketDetailPage() {
  const { ticketId } = useParams<{ ticketId: string }>();
  const queryClient = useQueryClient();
  const [reply, setReply] = useState("");
  const [error, setError] = useState<string | null>(null);

  const ticketQuery = useQuery({
    queryKey: ["admin-ticket", ticketId],
    queryFn: () => adminApi.getTicket(ticketId!),
    enabled: !!ticketId,
    refetchInterval: 10000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["admin-ticket", ticketId] });
    queryClient.invalidateQueries({ queryKey: ["admin-tickets"] });
    queryClient.invalidateQueries({ queryKey: ["admin-tickets-unread"] });
  };

  const replyMutation = useMutation({
    mutationFn: () => adminApi.replyToTicket(ticketId!, reply),
    onSuccess: () => {
      setReply("");
      setError(null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not send the reply."),
  });

  const statusMutation = useMutation({
    mutationFn: (status: SupportTicketStatus) => adminApi.setTicketStatus(ticketId!, status),
    onSuccess: invalidate,
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (reply.trim()) replyMutation.mutate();
  }

  if (ticketQuery.isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }

  const ticket = ticketQuery.data;
  if (!ticket) return <Card className="text-sm text-slate-500 dark:text-slate-400">Ticket not found.</Card>;

  return (
    <div className="space-y-6">
      <div>
        <Link to="/admin/tickets" className="text-sm text-brand-600 hover:text-brand-700">
          ← Back to tickets
        </Link>
        <div className="mt-1 flex items-center justify-between">
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">{ticket.subject}</h1>
          <Badge tone={STATUS_TONE[ticket.status]}>{ticket.status.replace("_", " ")}</Badge>
        </div>
        <p className="text-sm text-slate-500 dark:text-slate-400">{ticket.organization_name}</p>
      </div>

      <div className="flex gap-2">
        {STATUS_OPTIONS.filter((s) => s !== ticket.status).map((s) => (
          <Button
            key={s}
            variant="secondary"
            disabled={statusMutation.isPending}
            onClick={() => statusMutation.mutate(s)}
          >
            Mark {s.replace("_", " ")}
          </Button>
        ))}
      </div>

      <Card className="space-y-4">
        {ticket.messages.map((m) => (
          <div
            key={m.id}
            className={`rounded-lg p-3 text-sm ${
              m.author_is_admin
                ? "ml-8 bg-brand-50 dark:bg-brand-950"
                : "mr-8 bg-slate-50 dark:bg-slate-700/50"
            }`}
          >
            <div className="mb-1 text-xs font-medium text-slate-500 dark:text-slate-400">
              {m.author_is_admin ? "Support" : "Tenant"} · {new Date(m.created_at).toLocaleString()}
            </div>
            <p className="whitespace-pre-wrap text-slate-700 dark:text-slate-200">{m.body}</p>
          </div>
        ))}
      </Card>

      <Card>
        <form onSubmit={onSubmit} className="space-y-3">
          <textarea
            className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            rows={3}
            placeholder="Reply to this ticket…"
            value={reply}
            onChange={(e) => setReply(e.target.value)}
          />
          <ErrorText>{error}</ErrorText>
          <Button type="submit" disabled={!reply.trim() || replyMutation.isPending}>
            {replyMutation.isPending ? "Sending…" : "Send reply"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
