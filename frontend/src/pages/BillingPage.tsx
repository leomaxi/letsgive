import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { billingApi, orgApi, plansApi } from "@/lib/endpoints";
import type {
  BillingInterval,
  BillingOverview,
  CreditKind,
  Plan,
  PlanPriceQuote,
  SubscriptionStatus,
} from "@/lib/types";
import { Badge, Button, Card, ErrorText, Input, SegmentedControl, Spinner } from "@/components/ui";

const STATUS_TONE: Record<SubscriptionStatus, "green" | "amber" | "red" | "slate"> = {
  trialing: "amber",
  active: "green",
  past_due: "red",
  canceled: "slate",
};

const money = (cents: number) =>
  cents === 0
    ? "Free"
    : new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(cents / 100);
const moneyFromString = (amount: string) => money(Math.round(Number(amount) * 100));
const per = (value: BillingInterval) => (value === "yearly" ? "year" : "month");
const longDate = (value: string) =>
  new Date(value).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
const paymentsLabel = (count: number, value: BillingInterval) =>
  count === 1 ? `${value} payment` : `${count} ${value} payments`;

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
      {children}
    </h2>
  );
}

function UsageMeter({
  label,
  used,
  included,
  credits,
}: {
  label: string;
  used: number;
  included: number | null;
  credits: number;
}) {
  if (included === null) {
    return (
      <div>
        <div className="flex justify-between text-sm">
          <span className="font-medium text-slate-700 dark:text-slate-300">{label}</span>
          <span className="text-slate-500 dark:text-slate-400">{used} used · unlimited</span>
        </div>
      </div>
    );
  }
  const fromPlan = Math.min(used, included);
  const pct = included === 0 ? 100 : Math.min(100, Math.round((fromPlan / included) * 100));
  const left = Math.max(included - used, 0);
  const exhausted = left === 0;
  return (
    <div>
      <div className="flex flex-wrap justify-between gap-2 text-sm">
        <span className="font-medium text-slate-700 dark:text-slate-300">{label}</span>
        <span className="text-slate-500 dark:text-slate-400">
          {included === 0 ? "Not included in your plan" : `${fromPlan} of ${included} used this month`}
        </span>
      </div>
      <div
        className="mt-2 h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700"
        role="progressbar"
        aria-label={`${label} used`}
        aria-valuenow={fromPlan}
        aria-valuemin={0}
        aria-valuemax={included}
      >
        <div
          className={`h-full rounded-full ${exhausted ? "bg-amber-500" : "bg-brand-600"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
        {credits > 0
          ? `+ ${credits} extra ${credits === 1 ? label.toLowerCase().replace(/s$/, "") : label.toLowerCase()} you bought (never expire)`
          : exhausted && included > 0
            ? "All used — buy extras below or upgrade."
            : `${left} left this month`}
      </div>
    </div>
  );
}

function TopUpRow({
  kind,
  label,
  unitCents,
  disabled,
  onBuy,
  busy,
}: {
  kind: CreditKind;
  label: string;
  unitCents: number;
  disabled: boolean;
  onBuy: (kind: CreditKind, quantity: number) => void;
  busy: boolean;
}) {
  const [quantity, setQuantity] = useState("5");
  const qty = Number(quantity);
  const valid = Number.isInteger(qty) && qty >= 1 && qty <= 100;
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="min-w-[10rem] flex-1">
        <div className="text-sm font-medium text-slate-700 dark:text-slate-300">{label}</div>
        <div className="text-xs text-slate-500 dark:text-slate-400">{money(unitCents)} each</div>
      </div>
      <label className="sr-only" htmlFor={`topup-${kind}`}>
        Quantity of {label.toLowerCase()}
      </label>
      <div className="w-24">
        <Input
          id={`topup-${kind}`}
          type="number"
          min={1}
          max={100}
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
        />
      </div>
      <Button disabled={disabled || !valid || busy} onClick={() => onBuy(kind, qty)}>
        {busy ? "Opening checkout…" : valid ? `Buy for ${money(unitCents * qty)}` : "Buy"}
      </Button>
    </div>
  );
}

export default function BillingPage() {
  const { activeOrg, refetch } = useOrg();
  const queryClient = useQueryClient();
  const [interval, setInterval] = useState<BillingInterval>("monthly");
  const [confirmingPlanId, setConfirmingPlanId] = useState<string | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const handledReturn = useRef(false);

  const isOwner = activeOrg?.role === "owner";
  const canSeeBilling = !!activeOrg && (activeOrg.role === "owner" || activeOrg.role === "finance");
  const overviewKey = ["billing-overview", activeOrg?.id];

  const plansQuery = useQuery({ queryKey: ["plans"], queryFn: plansApi.list });
  const billingQuery = useQuery({
    queryKey: overviewKey,
    queryFn: () => billingApi.overview(activeOrg!.id),
    enabled: canSeeBilling,
  });
  const pricingQuery = useQuery({
    queryKey: ["billing-pricing", activeOrg?.id],
    queryFn: () => billingApi.pricing(activeOrg!.id),
    enabled: canSeeBilling,
  });

  function applyOverview(overview: BillingOverview) {
    queryClient.setQueryData(overviewKey, overview);
    refetch();
    queryClient.invalidateQueries({ queryKey: ["my-organizations"] });
  }

  function failWith(fallback: string) {
    return (err: unknown) => setError(err instanceof ApiError ? err.message : fallback);
  }

  // Back from Stripe Checkout (?stripe=... or ?topup=...): read the result
  // from Stripe through the server, which applies the plan or credits even
  // if Stripe's webhook hasn't arrived yet. Then tidy the URL.
  useEffect(() => {
    if (!activeOrg || !canSeeBilling || handledReturn.current) return;
    const params = new URLSearchParams(window.location.search);
    const stripeOutcome = params.get("stripe");
    const topupOutcome = params.get("topup");
    if (!stripeOutcome && !topupOutcome) return;
    handledReturn.current = true;
    const sessionId = params.get("session_id");
    const clearParams = () => {
      ["stripe", "topup", "session_id"].forEach((key) => params.delete(key));
      const qs = params.toString();
      window.history.replaceState(null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
    };
    if ((stripeOutcome === "success" || topupOutcome === "success") && sessionId) {
      setNotice("Confirming your payment…");
      billingApi
        .confirmCheckout(activeOrg.id, sessionId)
        .then((overview) => {
          applyOverview(overview);
          setNotice(
            topupOutcome
              ? "Payment received — your extra credits are ready to use."
              : "Payment received — your new plan is active. It renews automatically."
          );
        })
        .catch(failWith("We couldn't confirm the payment yet. Refresh in a minute."))
        .finally(clearParams);
    } else {
      setNotice("Checkout was canceled. Nothing was charged.");
      clearParams();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeOrg, canSeeBilling]);

  const checkoutMutation = useMutation({
    mutationFn: (planId: string) => billingApi.checkout(activeOrg!.id, planId, interval),
    onSuccess: (result) => {
      setConfirmingPlanId(null);
      setError(null);
      if (result.action === "redirect" && result.url) {
        window.location.assign(result.url);
        return;
      }
      if (result.overview) applyOverview(result.overview);
      setNotice("Your plan has been changed. Any difference was charged to your saved card.");
    },
    onError: (err) => {
      setConfirmingPlanId(null);
      failWith("Could not start checkout.")(err);
    },
  });

  const freePlanMutation = useMutation({
    mutationFn: (planId: string) => orgApi.switchPlan(activeOrg!.id, planId),
    onSuccess: () => {
      setConfirmingPlanId(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: overviewKey });
    },
    onError: (err) => {
      setConfirmingPlanId(null);
      failWith("Could not switch plans.")(err);
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => billingApi.cancelAutoRenew(activeOrg!.id),
    onSuccess: (overview) => {
      setConfirmingCancel(false);
      applyOverview(overview);
      setNotice("Auto-renew is off. You keep your plan until the end of the period you've paid for.");
    },
    onError: failWith("Could not cancel auto-renew."),
  });

  const resumeMutation = useMutation({
    mutationFn: () => billingApi.resumeAutoRenew(activeOrg!.id),
    onSuccess: (overview) => {
      applyOverview(overview);
      setNotice("Auto-renew is back on.");
    },
    onError: failWith("Could not turn auto-renew back on."),
  });

  const portalMutation = useMutation({
    mutationFn: () => billingApi.openPortal(activeOrg!.id),
    onSuccess: (result) => window.location.assign(result.url),
    onError: failWith("Could not open the payment settings."),
  });

  const topUpMutation = useMutation({
    mutationFn: ({ kind, quantity }: { kind: CreditKind; quantity: number }) =>
      billingApi.buyTopUp(activeOrg!.id, kind, quantity),
    onSuccess: (result) => window.location.assign(result.url),
    onError: failWith("Could not open checkout."),
  });

  const promotionMutation = useMutation({
    mutationFn: () => billingApi.applyPromotion(activeOrg!.id),
    onSuccess: (overview) => {
      applyOverview(overview);
      setNotice("Promotion applied to your upcoming renewals.");
    },
    onError: failWith("Could not apply the promotion."),
  });

  if (!activeOrg) return null;
  if (!canSeeBilling) {
    return (
      <Card className="text-sm text-slate-500 dark:text-slate-400">
        Only Owners and Finance officers can view billing.
      </Card>
    );
  }
  if (billingQuery.isLoading || plansQuery.isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }

  const overview = billingQuery.data;
  const plans = plansQuery.data ?? [];
  const currentPlan = plans.find((p) => p.id === activeOrg.plan_id);
  const subscription = overview?.subscription ?? null;
  const liveSubscription =
    subscription && ["active", "past_due", "suspended"].includes(subscription.status) ? subscription : null;
  const stripeSubscription = liveSubscription?.provider === "stripe" ? liveSubscription : null;
  const paymentsEnabled = overview?.payments_enabled ?? false;
  const annualSavings = pricingQuery.data?.annual_savings_percent;
  const quoteFor = (planId: string): PlanPriceQuote | undefined =>
    pricingQuery.data?.quotes.find((q) => q.plan_id === planId && q.interval === interval);
  const promotion = overview?.available_promotion ?? null;
  const usage = overview?.usage;
  const busy =
    checkoutMutation.isPending ||
    freePlanMutation.isPending ||
    cancelMutation.isPending ||
    resumeMutation.isPending ||
    portalMutation.isPending;

  function planAction(plan: Plan) {
    const isPaid = plan.monthly_price_cents > 0;
    const isCurrentPlan = plan.id === activeOrg!.plan_id;
    if (!isOwner || activeOrg!.subscription_status === "canceled") return null;
    if (!isPaid) {
      if (isCurrentPlan) return null;
      if (stripeSubscription) {
        return {
          label: stripeSubscription.cancel_at_period_end ? "Moves here at period end" : "Switch to free",
          confirm: "Cancel auto-renew? You keep your current plan until the period you've paid for ends, then move to the free plan.",
          run: () => cancelMutation.mutate(),
          disabled: stripeSubscription.cancel_at_period_end,
        };
      }
      return {
        label: "Switch to free",
        confirm: `Switch to ${plan.name} now?`,
        run: () => freePlanMutation.mutate(plan.id),
        disabled: false,
      };
    }
    if (stripeSubscription) {
      if (stripeSubscription.plan_id === plan.id && stripeSubscription.interval === interval) return null;
      const sameplan = stripeSubscription.plan_id === plan.id;
      return {
        label: sameplan ? `Switch to ${interval} billing` : `Switch to ${plan.name}`,
        confirm:
          "Your saved card is charged the prorated difference now (or credited if it's cheaper), and the plan changes immediately.",
        run: () => checkoutMutation.mutate(plan.id),
        disabled: !paymentsEnabled,
      };
    }
    return {
      label: isCurrentPlan ? "Pay for this plan" : "Subscribe",
      confirm: `Continue to secure checkout for ${plan.name}, billed ${interval} and renewing automatically?`,
      run: () => checkoutMutation.mutate(plan.id),
      disabled: !paymentsEnabled,
    };
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Billing</h1>

      {notice && (
        <div
          role="status"
          className="rounded-md border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300"
        >
          {notice}
        </div>
      )}
      <ErrorText>{error}</ErrorText>

      {!paymentsEnabled && isOwner && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          Online payments aren't switched on yet, so plans and extras can't be bought right now. Please contact
          support if you need to upgrade.
        </div>
      )}

      {liveSubscription?.status === "past_due" && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          <span>
            Your last payment didn't go through. Stripe will retry automatically — update your card now to avoid
            interruption.
          </span>
          <Button variant="secondary" disabled={busy} onClick={() => portalMutation.mutate()}>
            Update card
          </Button>
        </div>
      )}

      <Card>
        <SectionTitle>Your plan</SectionTitle>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="text-xl font-semibold text-slate-900 dark:text-slate-100">
                {currentPlan?.name ?? "—"}
              </span>
              <Badge tone={STATUS_TONE[activeOrg.subscription_status]}>
                {activeOrg.subscription_status.replace("_", " ")}
              </Badge>
            </div>
            {liveSubscription ? (
              <div className="mt-1 space-y-1 text-sm text-slate-600 dark:text-slate-300">
                <div>
                  {moneyFromString(liveSubscription.amount)} / {per(liveSubscription.interval)}
                  {liveSubscription.regular_amount &&
                    Number(liveSubscription.regular_amount) !== Number(liveSubscription.amount) && (
                      <span className="text-slate-500 dark:text-slate-400">
                        {" "}
                        (promotional; normally {moneyFromString(liveSubscription.regular_amount)})
                      </span>
                    )}
                </div>
                {liveSubscription.current_period_end && (
                  <div>
                    {liveSubscription.cancel_at_period_end ? (
                      <span className="text-amber-700 dark:text-amber-400">
                        Ends {longDate(liveSubscription.current_period_end)} — then you move to the free plan.
                      </span>
                    ) : (
                      <>Renews automatically on {longDate(liveSubscription.current_period_end)}.</>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                {currentPlan && currentPlan.monthly_price_cents === 0
                  ? "You're on the free plan. Upgrade any time below."
                  : "No paid subscription on file for this plan."}
              </p>
            )}
            {activeOrg.discount_percent > 0 && (
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                {activeOrg.discount_percent}% account discount applied to all prices.
              </p>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {stripeSubscription && (
              <Button variant="secondary" disabled={busy} onClick={() => portalMutation.mutate()}>
                Payment method & invoices
              </Button>
            )}
            {isOwner && stripeSubscription?.cancel_at_period_end && (
              <Button disabled={busy} onClick={() => resumeMutation.mutate()}>
                Turn auto-renew back on
              </Button>
            )}
          </div>
        </div>

        {promotion && promotion.promotion && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-brand-200 bg-brand-50 p-3 text-sm dark:border-brand-800 dark:bg-brand-900/30">
            <span className="text-slate-700 dark:text-slate-200">
              <span className="font-medium">{promotion.promotion.name}:</span> {promotion.promotion.percent_off}% off
              your next {paymentsLabel(promotion.intro_cycles, promotion.interval)} (
              {moneyFromString(promotion.intro_amount)} instead of {moneyFromString(promotion.regular_amount)}).
            </span>
            {isOwner && (
              <Button disabled={promotionMutation.isPending} onClick={() => promotionMutation.mutate()}>
                {promotionMutation.isPending ? "Applying…" : "Claim offer"}
              </Button>
            )}
          </div>
        )}

        {isOwner && stripeSubscription && !stripeSubscription.cancel_at_period_end && (
          <div className="mt-4 border-t border-slate-100 pt-4 dark:border-slate-700">
            {confirmingCancel ? (
              <div className="space-y-2">
                <p className="text-sm text-slate-600 dark:text-slate-300">
                  Turn off auto-renew? Nothing is refunded, and you keep{" "}
                  {currentPlan?.name ?? "your plan"} until{" "}
                  {stripeSubscription.current_period_end
                    ? longDate(stripeSubscription.current_period_end)
                    : "the end of this period"}
                  . After that you move to the free plan — your data and reports stay.
                </p>
                <div className="flex gap-2">
                  <Button variant="danger" disabled={busy} onClick={() => cancelMutation.mutate()}>
                    {cancelMutation.isPending ? "Canceling…" : "Yes, turn off auto-renew"}
                  </Button>
                  <Button variant="secondary" disabled={busy} onClick={() => setConfirmingCancel(false)}>
                    Keep my plan
                  </Button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                className="text-sm font-medium text-slate-500 underline-offset-2 hover:text-red-600 hover:underline dark:text-slate-400"
                onClick={() => setConfirmingCancel(true)}
              >
                Cancel subscription
              </button>
            )}
          </div>
        )}
      </Card>

      {usage && (
        <Card>
          <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
            <SectionTitle>This month</SectionTitle>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {/* Allowances reset at 00:00 UTC on the 1st; show that calendar date. */}
              Resets{" "}
              {new Date(usage.resets_at).toLocaleDateString(undefined, {
                year: "numeric",
                month: "long",
                day: "numeric",
                timeZone: "UTC",
              })}
            </span>
          </div>
          <div className="grid gap-6 md:grid-cols-2">
            <UsageMeter
              label="Sessions"
              used={usage.sessions_used}
              included={usage.sessions_included}
              credits={usage.session_credits}
            />
            <UsageMeter
              label="Exports"
              used={usage.exports_used}
              included={usage.exports_included}
              credits={usage.export_credits}
            />
          </div>
          {paymentsEnabled && overview && (
            <div className="mt-6 space-y-4 border-t border-slate-100 pt-4 dark:border-slate-700">
              <div>
                <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-200">Need more this month?</h3>
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  Extras never expire. They're only used once the sessions or exports included in your plan run out.
                </p>
              </div>
              <TopUpRow
                kind="sessions"
                label="Extra sessions"
                unitCents={overview.session_topup_price_cents}
                disabled={overview.session_topup_price_cents <= 0}
                busy={topUpMutation.isPending && topUpMutation.variables?.kind === "sessions"}
                onBuy={(kind, quantity) => topUpMutation.mutate({ kind, quantity })}
              />
              <TopUpRow
                kind="exports"
                label="Extra exports"
                unitCents={overview.export_topup_price_cents}
                disabled={overview.export_topup_price_cents <= 0}
                busy={topUpMutation.isPending && topUpMutation.variables?.kind === "exports"}
                onBuy={(kind, quantity) => topUpMutation.mutate({ kind, quantity })}
              />
            </div>
          )}
        </Card>
      )}

      <Card>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <SectionTitle>Plans</SectionTitle>
          <SegmentedControl<BillingInterval>
            value={interval}
            onChange={setInterval}
            options={[
              { value: "monthly", label: "Monthly" },
              { value: "yearly", label: annualSavings ? `Yearly · save ${annualSavings}%` : "Yearly" },
            ]}
          />
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {plans.map((plan) => {
            const isCurrent =
              plan.id === activeOrg.plan_id && (!stripeSubscription || stripeSubscription.interval === interval || plan.monthly_price_cents === 0);
            const quote = quoteFor(plan.id);
            const isPaid = plan.monthly_price_cents > 0;
            const action = planAction(plan);
            return (
              <div
                key={plan.id}
                className={`flex flex-col rounded-lg border p-4 ${
                  isCurrent ? "border-brand-500 ring-1 ring-brand-500" : "border-slate-200 dark:border-slate-700"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-slate-900 dark:text-slate-100">{plan.name}</span>
                  {isCurrent && <Badge tone="blue">Current</Badge>}
                </div>
                {quote && isPaid ? (
                  <>
                    <div className="mt-1 text-lg font-semibold text-slate-900 dark:text-slate-100">
                      {quote.promotion && (
                        <span className="mr-1 text-sm font-normal text-slate-400 line-through dark:text-slate-500">
                          {moneyFromString(quote.regular_amount)}
                        </span>
                      )}
                      {moneyFromString(quote.intro_amount)}
                      <span className="text-xs font-normal text-slate-500 dark:text-slate-400"> / {per(interval)}</span>
                    </div>
                    {interval === "yearly" && Number(quote.savings_amount) > 0 && (
                      <div className="text-xs text-green-600 dark:text-green-400">
                        Save {moneyFromString(quote.savings_amount)} a year
                      </div>
                    )}
                    {quote.promotion && (
                      <div className="mt-1 text-xs text-brand-700 dark:text-brand-400">
                        {quote.promotion.name}: {quote.promotion.percent_off}% off your first{" "}
                        {paymentsLabel(quote.intro_cycles, interval)}
                      </div>
                    )}
                    <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">Renews automatically</div>
                  </>
                ) : (
                  <div className="mt-1 text-lg font-semibold text-slate-900 dark:text-slate-100">
                    {money(plan.monthly_price_cents)}
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
                        ? "Exports available as paid extras"
                        : `${plan.max_exports_per_month} transaction exports / month`}
                  </li>
                  <li>{plan.max_display_templates} display templates</li>
                  <li>{plan.max_team_members} team seats</li>
                  {plan.key === "starter" && <li>Projection watermark</li>}
                  {plan.allows_custom_subdomain && <li>Custom subdomain</li>}
                  {plan.allows_sso && <li>SSO</li>}
                </ul>
                {action && (
                  <div className="mt-3">
                    {confirmingPlanId === plan.id ? (
                      <div className="space-y-2">
                        <p className="text-xs text-slate-600 dark:text-slate-300">{action.confirm}</p>
                        <div className="flex gap-2">
                          <Button disabled={busy} onClick={action.run}>
                            {busy ? "Working…" : "Continue"}
                          </Button>
                          <Button variant="secondary" disabled={busy} onClick={() => setConfirmingPlanId(null)}>
                            Back
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <Button
                        variant={isPaid ? "primary" : "secondary"}
                        disabled={action.disabled || busy}
                        onClick={() => setConfirmingPlanId(plan.id)}
                      >
                        {action.label}
                      </Button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {activeOrg.subscription_status === "canceled" && (
          <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
            This organization has been deactivated — contact support to reactivate it.
          </p>
        )}
      </Card>

      {overview && (overview.payments.length > 0 || overview.refunds.length > 0) && (
        <Card>
          <SectionTitle>Payment history</SectionTitle>
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
            {overview.payments.map((payment) => (
              <li key={payment.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div className="min-w-0">
                  <div className="text-slate-700 dark:text-slate-200">
                    {payment.description ?? (payment.kind === "subscription" ? "Subscription" : "Payment")}
                  </div>
                  <div className="text-xs text-slate-500 dark:text-slate-400">{longDate(payment.created_at)}</div>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-slate-900 dark:text-slate-100">
                    {payment.currency} {Number(payment.amount).toFixed(2)}
                  </span>
                  {payment.status !== "completed" && <Badge tone="amber">{payment.status.replace("_", " ")}</Badge>}
                  {payment.receipt_url && (
                    <a
                      href={payment.receipt_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400"
                    >
                      Receipt
                    </a>
                  )}
                </div>
              </li>
            ))}
            {overview.refunds.map((refund) => (
              <li key={refund.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div>
                  <div className="text-slate-700 dark:text-slate-200">Refund</div>
                  <div className="text-xs text-slate-500 dark:text-slate-400">{longDate(refund.created_at)}</div>
                </div>
                <span className="text-slate-900 dark:text-slate-100">
                  −{refund.currency} {Number(refund.amount).toFixed(2)} · {refund.status}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
