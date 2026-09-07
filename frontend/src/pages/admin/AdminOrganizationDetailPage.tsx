import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { adminApi, plansApi } from "@/lib/endpoints";
import type { SubscriptionStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SubscriptionStatus, "green" | "amber" | "red" | "slate"> = {
  trialing: "amber",
  active: "green",
  past_due: "red",
  canceled: "slate",
};

const STATUS_OPTIONS: SubscriptionStatus[] = ["trialing", "active", "past_due", "canceled"];

export default function AdminOrganizationDetailPage() {
  const { orgId } = useParams<{ orgId: string }>();
  const queryClient = useQueryClient();
  const [planId, setPlanId] = useState("");
  const [subscriptionStatus, setSubscriptionStatus] = useState("");
  const [error, setError] = useState<string | null>(null);

  const orgQuery = useQuery({
    queryKey: ["admin-organization", orgId],
    queryFn: () => adminApi.getOrganization(orgId!),
    enabled: !!orgId,
  });
  const plansQuery = useQuery({ queryKey: ["plans"], queryFn: plansApi.list });

  const updateMutation = useMutation({
    mutationFn: () =>
      adminApi.updateSubscription(orgId!, {
        plan_id: planId || undefined,
        subscription_status: (subscriptionStatus || undefined) as SubscriptionStatus | undefined,
      }),
    onSuccess: () => {
      setPlanId("");
      setSubscriptionStatus("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["admin-organization", orgId] });
      queryClient.invalidateQueries({ queryKey: ["admin-organizations"] });
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : "Could not update the subscription."),
  });

  if (orgQuery.isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }

  const org = orgQuery.data;
  if (!org) return <Card className="text-sm text-slate-500 dark:text-slate-400">Organization not found.</Card>;

  return (
    <div className="space-y-6">
      <div>
        <Link to="/admin/organizations" className="text-sm text-brand-600 hover:text-brand-700">
          ← Back to organizations
        </Link>
        <h1 className="mt-1 text-2xl font-semibold text-slate-900 dark:text-slate-100">{org.name}</h1>
      </div>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Subscription
        </h2>
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Plan</dt>
            <dd className="text-slate-700 dark:text-slate-300">{org.plan_name ?? "—"}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Status</dt>
            <dd>
              <Badge tone={STATUS_TONE[org.subscription_status]}>
                {org.subscription_status.replace("_", " ")}
              </Badge>
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Members</dt>
            <dd className="text-slate-700 dark:text-slate-300">{org.member_count}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Mailbox connections</dt>
            <dd className="text-slate-700 dark:text-slate-300">
              {org.connections_connected} connected / {org.connections_total} total
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Created</dt>
            <dd className="text-slate-700 dark:text-slate-300">
              {new Date(org.created_at).toLocaleDateString()}
            </dd>
          </div>
          {org.grace_period_ends_at && (
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">Reports readable until</dt>
              <dd className="text-slate-700 dark:text-slate-300">
                {new Date(org.grace_period_ends_at).toLocaleDateString()}
              </dd>
            </div>
          )}
        </dl>

        <div className="mt-4 space-y-3 border-t border-slate-100 pt-4 dark:border-slate-700">
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Manual override for support/billing troubleshooting. Bypasses the usual
            self-service restriction that blocks changing plans on a canceled subscription --
            set a status here to reactivate one deliberately.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Change plan to" htmlFor="planId">
              <select
                id="planId"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
                value={planId}
                onChange={(e) => setPlanId(e.target.value)}
              >
                <option value="">No change</option>
                {plansQuery.data?.map((plan) => (
                  <option key={plan.id} value={plan.id}>
                    {plan.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Change status to" htmlFor="subStatus">
              <select
                id="subStatus"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
                value={subscriptionStatus}
                onChange={(e) => setSubscriptionStatus(e.target.value)}
              >
                <option value="">No change</option>
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s.replace("_", " ")}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <ErrorText>{error}</ErrorText>
          <Button
            disabled={(!planId && !subscriptionStatus) || updateMutation.isPending}
            onClick={() => updateMutation.mutate()}
          >
            {updateMutation.isPending ? "Saving…" : "Apply changes"}
          </Button>
        </div>
      </Card>
    </div>
  );
}
