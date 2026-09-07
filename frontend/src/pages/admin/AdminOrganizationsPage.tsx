import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { adminApi } from "@/lib/endpoints";
import type { SubscriptionStatus } from "@/lib/types";
import { Badge, Card, Input, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SubscriptionStatus, "green" | "amber" | "red" | "slate"> = {
  trialing: "amber",
  active: "green",
  past_due: "red",
  canceled: "slate",
};

const PAGE_SIZE = 50;

export default function AdminOrganizationsPage() {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);

  const query = useQuery({
    queryKey: ["admin-organizations", offset, search],
    queryFn: () => adminApi.listOrganizations(PAGE_SIZE, offset, search || undefined),
  });

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Organizations</h1>

      <Input
        placeholder="Search by name…"
        value={search}
        onChange={(e) => {
          setSearch(e.target.value);
          setOffset(0);
        }}
        className="max-w-sm"
      />

      {query.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : (
        <Card className="p-0">
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
            {query.data?.map((org) => (
              <li key={org.id}>
                <Link
                  to={`/admin/organizations/${org.id}`}
                  className="flex items-center justify-between px-6 py-3 hover:bg-slate-50 dark:hover:bg-slate-700/50"
                >
                  <div>
                    <div className="font-medium text-slate-700 dark:text-slate-300">{org.name}</div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">
                      {org.plan_name ?? "No plan"} · {org.member_count} member
                      {org.member_count === 1 ? "" : "s"} · created{" "}
                      {new Date(org.created_at).toLocaleDateString()}
                    </div>
                  </div>
                  <Badge tone={STATUS_TONE[org.subscription_status]}>
                    {org.subscription_status.replace("_", " ")}
                  </Badge>
                </Link>
              </li>
            ))}
          </ul>
          {query.data?.length === 0 && (
            <p className="px-6 py-4 text-sm text-slate-500 dark:text-slate-400">
              No organizations match.
            </p>
          )}
        </Card>
      )}

      <div className="flex justify-between">
        <button
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          className="text-sm text-brand-600 hover:text-brand-700 disabled:opacity-40 disabled:hover:text-brand-600"
        >
          ← Previous
        </button>
        <button
          disabled={!query.data || query.data.length < PAGE_SIZE}
          onClick={() => setOffset(offset + PAGE_SIZE)}
          className="text-sm text-brand-600 hover:text-brand-700 disabled:opacity-40 disabled:hover:text-brand-600"
        >
          Next →
        </button>
      </div>
    </div>
  );
}
