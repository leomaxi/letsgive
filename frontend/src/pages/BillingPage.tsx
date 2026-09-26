import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { billingApi, orgApi, plansApi } from "@/lib/endpoints";
import type { SubscriptionStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SubscriptionStatus, "green" | "amber" | "red" | "slate"> = {
  trialing: "amber",
  active: "green",
  past_due: "red",
  canceled: "slate",
};

export default function BillingPage() {
  const { activeOrg, refetch } = useOrg();
  const queryClient = useQueryClient();
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [confirmingPlanId, setConfirmingPlanId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const plansQuery = useQuery({ queryKey: ["plans"], queryFn: plansApi.list });
  const billingQuery = useQuery({
    queryKey: ["billing-overview", activeOrg?.id],
    queryFn: () => billingApi.overview(activeOrg!.id),
    enabled: !!activeOrg && (activeOrg.role === "owner" || activeOrg.role === "finance"),
  });

  const cancelMutation = useMutation({
    mutationFn: () => orgApi.cancelSubscription(activeOrg!.id),
    onSuccess: () => {
      setConfirmingCancel(false);
      setError(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ["my-organizations"] });
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : "Could not cancel the subscription.");
    },
  });

  const switchPlanMutation = useMutation({
    mutationFn: (planId: string) => orgApi.switchPlan(activeOrg!.id, planId),
    onSuccess: () => {
      setConfirmingPlanId(null);
      setError(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ["my-organizations"] });
    },
    onError: (err) => {
      setConfirmingPlanId(null);
      setError(err instanceof ApiError ? err.message : "Could not switch plans.");
    },
  });

  const startPayPalMutation = useMutation({
    mutationFn: (planId: string) => billingApi.startPayPalSubscription(activeOrg!.id, planId),
    onSuccess: (result) => {
      setConfirmingPlanId(null);
      setError(null);
      window.location.assign(result.approval_url);
    },
    onError: (err) => {
      setConfirmingPlanId(null);
      setError(err instanceof ApiError ? err.message : "Could not start PayPal subscription.");
    },
  });

  if (!activeOrg) return null;

  if (activeOrg.role !== "owner" && activeOrg.role !== "finance") {
    return (
      <Card className="text-sm text-slate-500 dark:text-slate-400">
        Only Owners and Finance officers can view billing.
      </Card>
    );
  }

  const currentPlan = plansQuery.data?.find((p) => p.id === activeOrg.plan_id);
  const isCanceled = activeOrg.subscription_status === "canceled";
  const discountedCents = (cents: number) =>
    Math.round(cents * (100 - (activeOrg.discount_percent ?? 0)) / 100);
  const formatPrice = (cents: number) =>
    cents === 0
      ? "Free"
      : new Intl.NumberFormat(undefined, {
          style: "currency",
          currency: "USD",
        }).format(cents / 100);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Billing</h1>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Current subscription
        </h2>
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Plan</dt>
            <dd className="text-slate-700 dark:text-slate-300">{currentPlan?.name ?? "—"}</dd>
          </div>
          {currentPlan && (
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">Amount</dt>
              <dd className="text-slate-700 dark:text-slate-300">
                {formatPrice(discountedCents(currentPlan.monthly_price_cents))}
                {currentPlan.monthly_price_cents > 0 && " / month"}
              </dd>
            </div>
          )}
          {activeOrg.discount_percent > 0 && (
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">Discount</dt>
              <dd className="text-slate-700 dark:text-slate-300">{activeOrg.discount_percent}%</dd>
            </div>
          )}
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Status</dt>
            <dd>
              <Badge tone={STATUS_TONE[activeOrg.subscription_status]}>
                {activeOrg.subscription_status.replace("_", " ")}
              </Badge>
            </dd>
          </div>
          {activeOrg.grace_period_ends_at && (
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">Reports readable until</dt>
              <dd className="text-slate-700 dark:text-slate-300">
                {new Date(activeOrg.grace_period_ends_at).toLocaleDateString()}
              </dd>
            </div>
          )}
          {billingQuery.data?.subscription && (
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">PayPal subscription</dt>
              <dd className="text-slate-700 dark:text-slate-300">
                {billingQuery.data.subscription.status.replace("_", " ")}
              </dd>
            </div>
          )}
          {billingQuery.data?.subscription?.current_period_end && (
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">Paid through</dt>
              <dd className="text-slate-700 dark:text-slate-300">
                {new Date(billingQuery.data.subscription.current_period_end).toLocaleDateString()}
              </dd>
            </div>
          )}
        </dl>

        {activeOrg.role === "owner" && !isCanceled && (
          <div className="mt-4 border-t border-slate-100 pt-4 dark:border-slate-700">
            <ErrorText>{error}</ErrorText>
            {confirmingCancel ? (
              <div className="space-y-2">
                <p className="text-sm text-slate-600 dark:text-slate-300">
                  Canceling stops new sessions, connections, and templates immediately. Existing
                  reports stay readable for 30 days. This can't be undone from here.
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="danger"
                    disabled={cancelMutation.isPending}
                    onClick={() => cancelMutation.mutate()}
                  >
                    {cancelMutation.isPending ? "Canceling…" : "Yes, cancel subscription"}
                  </Button>
                  <Button
                    variant="secondary"
                    disabled={cancelMutation.isPending}
                    onClick={() => setConfirmingCancel(false)}
                  >
                    Never mind
                  </Button>
                </div>
              </div>
            ) : (
              <Button variant="danger" onClick={() => setConfirmingCancel(true)}>
                Cancel subscription
              </Button>
            )}
          </div>
        )}
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Plans
        </h2>
        {plansQuery.isLoading ? (
          <Spinner className="h-5 w-5 text-brand-600" />
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {plansQuery.data?.map((plan) => {
              const isCurrent = plan.id === activeOrg.plan_id;
              return (
                <div
                  key={plan.id}
                  className={`flex flex-col rounded-lg border p-4 ${
                    isCurrent
                      ? "border-brand-500 ring-1 ring-brand-500"
                      : "border-slate-200 dark:border-slate-700"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-slate-900 dark:text-slate-100">{plan.name}</span>
                    {isCurrent && <Badge tone="blue">Current</Badge>}
                  </div>
                  <div className="mt-1 text-lg font-semibold text-slate-900 dark:text-slate-100">
                    {formatPrice(discountedCents(plan.monthly_price_cents))}
                    {plan.monthly_price_cents > 0 && (
                      <span className="text-xs font-normal text-slate-500 dark:text-slate-400"> / month</span>
                    )}
                  </div>
                  {activeOrg.discount_percent > 0 && plan.monthly_price_cents > 0 && (
                    <div className="text-xs text-slate-500 dark:text-slate-400">
                      {activeOrg.discount_percent}% account discount applied
                    </div>
                  )}
                  <ul className="mt-3 flex-1 space-y-1 text-xs text-slate-500 dark:text-slate-400">
                    <li>{plan.max_sessions_per_month} sessions / month</li>
                    <li>{plan.max_mailbox_connections} mailbox connections</li>
                    <li>
                      {plan.max_session_mailbox_connections === null
                        ? "Unlimited emails per session"
                        : `${plan.max_session_mailbox_connections} email${plan.max_session_mailbox_connections === 1 ? "" : "s"} per session`}
                    </li>
                    <li>
                      {plan.max_exports_per_month === null
                        ? "Unlimited transaction exports"
                        : plan.max_exports_per_month === 0
                          ? "No transaction exports"
                          : `${plan.max_exports_per_month} transaction exports / month`}
                    </li>
                    <li>{plan.max_display_templates} display templates</li>
                    <li>{plan.max_team_members} team seats</li>
                    {plan.key === "starter" && <li>Projection watermark required</li>}
                    {plan.allows_custom_subdomain && <li>Custom subdomain</li>}
                    {plan.allows_sso && <li>SSO</li>}
                  </ul>
                  {activeOrg.role === "owner" && !isCurrent && !isCanceled && (
                    <div className="mt-3">
                      {confirmingPlanId === plan.id ? (
                        <div className="space-y-2">
                          <p className="text-xs text-slate-600 dark:text-slate-300">Switch to {plan.name} now?</p>
                          <div className="flex gap-2">
                            <Button
                              disabled={switchPlanMutation.isPending || startPayPalMutation.isPending}
                              onClick={() =>
                                plan.monthly_price_cents > 0
                                  ? startPayPalMutation.mutate(plan.id)
                                  : switchPlanMutation.mutate(plan.id)
                              }
                            >
                              {switchPlanMutation.isPending || startPayPalMutation.isPending
                                ? "Working…"
                                : plan.monthly_price_cents > 0
                                  ? "Continue to PayPal"
                                  : "Confirm"}
                            </Button>
                            <Button
                              variant="secondary"
                              disabled={switchPlanMutation.isPending || startPayPalMutation.isPending}
                              onClick={() => setConfirmingPlanId(null)}
                            >
                              Cancel
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <Button variant="secondary" onClick={() => setConfirmingPlanId(plan.id)}>
                          Switch to this plan
                        </Button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {isCanceled && (
          <p className="mt-4 text-xs text-slate-400 dark:text-slate-500">
            This subscription is canceled — contact support to reactivate it before switching plans.
          </p>
        )}
      </Card>

      {(billingQuery.data?.payments.length || billingQuery.data?.refunds.length) ? (
        <Card>
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            PayPal activity
          </h2>
          <div className="space-y-3 text-sm">
            {billingQuery.data.payments.slice(0, 5).map((payment) => (
              <div key={payment.id} className="flex justify-between border-b border-slate-100 pb-2 dark:border-slate-700">
                <span className="text-slate-600 dark:text-slate-300">
                  Payment {new Date(payment.created_at).toLocaleDateString()}
                </span>
                <span className="text-slate-900 dark:text-slate-100">
                  {payment.currency} {payment.amount} · {payment.status.replace("_", " ")}
                </span>
              </div>
            ))}
            {billingQuery.data.refunds.slice(0, 5).map((refund) => (
              <div key={refund.id} className="flex justify-between border-b border-slate-100 pb-2 dark:border-slate-700">
                <span className="text-slate-600 dark:text-slate-300">
                  Refund {new Date(refund.created_at).toLocaleDateString()}
                </span>
                <span className="text-slate-900 dark:text-slate-100">
                  {refund.currency} {refund.amount} · {refund.status}
                </span>
              </div>
            ))}
          </div>
        </Card>
      ) : null}
    </div>
  );
}
