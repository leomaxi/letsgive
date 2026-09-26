import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { adminApi, plansApi } from "@/lib/endpoints";
import type { SubscriptionStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Input, Spinner } from "@/components/ui";

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
  const [planStartsAt, setPlanStartsAt] = useState("");
  const [planExpiresAt, setPlanExpiresAt] = useState("");
  const [discountPercent, setDiscountPercent] = useState("");
  const [bonusSessions, setBonusSessions] = useState("");
  const [bonusExpiresOn, setBonusExpiresOn] = useState("");
  const [bonusError, setBonusError] = useState<string | null>(null);
  const [refundReason, setRefundReason] = useState("");
  const [refundMessage, setRefundMessage] = useState<string | null>(null);
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
        plan_starts_at: planStartsAt || undefined,
        plan_expires_at: planExpiresAt || undefined,
        discount_percent: discountPercent === "" ? undefined : Number(discountPercent),
      }),
    onSuccess: () => {
      setPlanId("");
      setSubscriptionStatus("");
      setPlanStartsAt("");
      setPlanExpiresAt("");
      setDiscountPercent("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["admin-organization", orgId] });
      queryClient.invalidateQueries({ queryKey: ["admin-organizations"] });
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : "Could not update the subscription."),
  });

  const bonusMutation = useMutation({
    mutationFn: () =>
      adminApi.grantBonusSessions(
        orgId!,
        Number(bonusSessions),
        new Date(`${bonusExpiresOn}T23:59:59`).toISOString()
      ),
    onSuccess: () => {
      setBonusSessions("");
      setBonusExpiresOn("");
      setBonusError(null);
      queryClient.invalidateQueries({ queryKey: ["admin-organization", orgId] });
    },
    onError: (err) =>
      setBonusError(err instanceof ApiError ? err.message : "Could not grant bonus sessions."),
  });

  const refundMutation = useMutation({
    mutationFn: () => adminApi.processRefund(orgId!, refundReason),
    onSuccess: (refund) => {
      setRefundReason("");
      setRefundMessage(`Refund ${refund.currency} ${refund.amount} submitted to PayPal.`);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["admin-organization", orgId] });
    },
    onError: (err) => {
      setRefundMessage(null);
      setError(err instanceof ApiError ? err.message : "Could not process the refund.");
    },
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
          {org.plan_expires_at && (
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">Plan scheduled to expire</dt>
              <dd className="text-amber-600 dark:text-amber-400">
                {new Date(org.plan_expires_at).toLocaleDateString()} — reverts to Starter if not
                renewed
              </dd>
            </div>
          )}
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Members</dt>
            <dd className="text-slate-700 dark:text-slate-300">{org.member_count}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-slate-500 dark:text-slate-400">Discount</dt>
            <dd className="text-slate-700 dark:text-slate-300">{org.discount_percent}%</dd>
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
          <Field label="Account discount (%)" htmlFor="discountPercent">
            <Input
              id="discountPercent"
              type="number"
              min={0}
              max={100}
              value={discountPercent}
              onChange={(e) => setDiscountPercent(e.target.value)}
              placeholder={`${org.discount_percent}`}
            />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Plan starts" htmlFor="planStartsAt">
              <Input
                id="planStartsAt"
                type="date"
                value={planStartsAt}
                onChange={(e) => setPlanStartsAt(e.target.value)}
              />
            </Field>
            <Field label="Plan expires (optional)" htmlFor="planExpiresAt">
              <Input
                id="planExpiresAt"
                type="date"
                value={planExpiresAt}
                onChange={(e) => setPlanExpiresAt(e.target.value)}
              />
            </Field>
          </div>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            If a plan is being granted for a limited time, set an expiry — the organization reverts
            to the Starter plan automatically if nobody renews it before then. Leave blank for an
            open-ended grant.
          </p>
          <ErrorText>{error}</ErrorText>
          <Button
            disabled={
              (!planId && !subscriptionStatus && !planStartsAt && !planExpiresAt && !discountPercent) ||
              updateMutation.isPending
            }
            onClick={() => updateMutation.mutate()}
          >
            {updateMutation.isPending ? "Saving…" : "Apply changes"}
          </Button>
        </div>
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Bonus sessions
        </h2>
        {(() => {
          const active =
            org.bonus_sessions > 0 &&
            org.bonus_sessions_expires_at !== null &&
            new Date(org.bonus_sessions_expires_at).getTime() > Date.now();
          return (
            <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
              {active
                ? `${org.bonus_sessions} extra session${org.bonus_sessions === 1 ? "" : "s"} per month on top of the plan limit, until ${new Date(org.bonus_sessions_expires_at!).toLocaleDateString()}.`
                : "No bonus sessions active."}
            </p>
          );
        })()}
        <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
          Adds sessions on top of the plan's monthly limit. They must expire within the current
          subscription period
          {org.subscription_period_ends_at
            ? ` (ends ${new Date(org.subscription_period_ends_at).toLocaleDateString()})`
            : " — this organization has no paid period end yet, so bonus sessions can't be granted"}
          . Granting again replaces the current grant; set 0 to remove it.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Extra sessions" htmlFor="bonusSessions">
            <Input
              id="bonusSessions"
              type="number"
              min={0}
              max={10000}
              value={bonusSessions}
              onChange={(e) => setBonusSessions(e.target.value)}
            />
          </Field>
          <Field label="Expires on" htmlFor="bonusExpiresOn">
            <Input
              id="bonusExpiresOn"
              type="date"
              max={org.subscription_period_ends_at?.slice(0, 10)}
              value={bonusExpiresOn}
              onChange={(e) => setBonusExpiresOn(e.target.value)}
            />
          </Field>
        </div>
        <ErrorText>{bonusError}</ErrorText>
        <div className="mt-3">
          <Button
            disabled={
              bonusSessions === "" ||
              !Number.isInteger(Number(bonusSessions)) ||
              Number(bonusSessions) < 0 ||
              !bonusExpiresOn ||
              bonusMutation.isPending
            }
            onClick={() => bonusMutation.mutate()}
          >
            {bonusMutation.isPending ? "Saving…" : "Grant bonus sessions"}
          </Button>
        </div>
      </Card>

      <Card>
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Refund
        </h2>
        <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
          Processes a PayPal refund for the unused portion of the latest monthly payment. A reason is
          required and will be stored in the audit trail.
        </p>
        <Field label="Refund reason" htmlFor="refundReason">
          <textarea
            id="refundReason"
            className="min-h-24 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            value={refundReason}
            onChange={(e) => setRefundReason(e.target.value)}
            maxLength={1000}
          />
        </Field>
        {refundMessage && <p className="mt-2 text-sm text-green-600 dark:text-green-400">{refundMessage}</p>}
        <ErrorText>{error}</ErrorText>
        <div className="mt-3">
          <Button
            variant="danger"
            disabled={refundReason.trim().length < 3 || refundMutation.isPending}
            onClick={() => refundMutation.mutate()}
          >
            {refundMutation.isPending ? "Processing…" : "Process prorated refund"}
          </Button>
        </div>
      </Card>
    </div>
  );
}
