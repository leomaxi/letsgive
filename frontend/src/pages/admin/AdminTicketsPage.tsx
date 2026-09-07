import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { adminApi } from "@/lib/endpoints";
import type { SupportTicketStatus } from "@/lib/types";
import { Badge, Card, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SupportTicketStatus, "amber" | "blue" | "green" | "slate"> = {
  open: "amber",
  in_progress: "blue",
  resolved: "green",
  closed: "slate",
};

const STATUS_FILTERS: { label: string; value: SupportTicketStatus | undefined }[] = [
  { label: "Open", value: "open" },
  { label: "In progress", value: "in_progress" },
  { label: "Resolved", value: "resolved" },
  { label: "Closed", value: "closed" },
  { label: "All", value: undefined },
];

export default function AdminTicketsPage() {
  const [statusFilter, setStatusFilter] = useState<SupportTicketStatus | undefined>("open");

  const query = useQuery({
    queryKey: ["admin-tickets", statusFilter],
    queryFn: () => adminApi.listTickets(statusFilter),
    refetchInterval: 10000,
  });

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Support tickets</h1>

      <div className="flex gap-2 text-sm">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.label}
            onClick={() => setStatusFilter(f.value)}
            className={`rounded-md px-3 py-1.5 font-medium ${
              statusFilter === f.value
                ? "bg-brand-600 text-white"
                : "bg-white text-slate-600 border border-slate-300 hover:bg-slate-50 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-600 dark:hover:bg-slate-700"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {query.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : (
        <Card className="p-0">
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
            {query.data?.map((ticket) => (
              <li key={ticket.id}>
                <Link
                  to={`/admin/tickets/${ticket.id}`}
                  className="flex items-center justify-between px-6 py-3 hover:bg-slate-50 dark:hover:bg-slate-700/50"
                >
                  <div>
                    <div className="flex items-center gap-2 font-medium text-slate-700 dark:text-slate-300">
                      {ticket.admin_unread && (
                        <span className="h-2 w-2 flex-shrink-0 rounded-full bg-brand-600" aria-label="Unread" />
                      )}
                      {ticket.subject}
                    </div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">
                      {ticket.organization_name} · updated{" "}
                      {new Date(ticket.updated_at).toLocaleString()}
                    </div>
                  </div>
                  <Badge tone={STATUS_TONE[ticket.status]}>{ticket.status.replace("_", " ")}</Badge>
                </Link>
              </li>
            ))}
          </ul>
          {query.data?.length === 0 && (
            <p className="px-6 py-4 text-sm text-slate-500 dark:text-slate-400">No tickets here.</p>
          )}
        </Card>
      )}
    </div>
  );
}
