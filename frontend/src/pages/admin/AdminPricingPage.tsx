import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { adminApi, plansApi } from "@/lib/endpoints";
import type { BillingPromotion, Plan } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Input, Select, Spinner, Textarea } from "@/components/ui";

const formatUsd = (cents: number) =>
  new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(cents / 100);

// Date inputs give a local calendar day; promotions run from the start of the
// first day to the end of the last one in the admin's own timezone.
const startOfDay = (date: string) => new Date(`${date}T00:00:00`).toISOString();
const endOfDay = (date: string) => new Date(`${date}T23:59:59`).toISOString();

function promotionState(promotion: BillingPromotion): { label: string; tone: "green" | "amber" | "slate" } {
  const now = Date.now();
  if (!promotion.is_active) return { label: "disabled", tone: "slate" };
  if (new Date(promotion.ends_at).getTime() < now) return { label: "ended", tone: "slate" };
  if (new Date(promotion.starts_at).getTime() > now) return { label: "scheduled", tone: "amber" };
  return { label: "running", tone: "green" };
}

const WEBHOOK_EVENTS = [
  "checkout.session.completed",
  "checkout.session.async_payment_succeeded",
  "customer.subscription.created",
  "customer.subscription.updated",
  "customer.subscription.deleted",
  "invoice.paid",
  "invoice.payment_failed",
];

function StripeSettingsCard() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({ queryKey: ["admin-payment-settings"], queryFn: adminApi.getPaymentSettings });
  const [secretKey, setSecretKey] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [sessionPrice, setSessionPrice] = useState("");
  const [exportPrice, setExportPrice] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const webhookUrl = `${window.location.origin}/v1/stripe/webhook`;

  const saveMutation = useMutation({
    mutationFn: (payload: Parameters<typeof adminApi.updatePaymentSettings>[0]) =>
      adminApi.updatePaymentSettings(payload),
    onSuccess: (data) => {
      queryClient.setQueryData(["admin-payment-settings"], data);
      setSecretKey("");
      setWebhookSecret("");
      setSessionPrice("");
      setExportPrice("");
      setError(null);
      setMessage("Saved.");
    },
    onError: (err) => {
      setMessage(null);
      setError(err instanceof ApiError ? err.message : "Could not save.");
    },
  });

  const testMutation = useMutation({
    mutationFn: adminApi.testStripe,
    onSuccess: (result) => {
      if (result.ok) {
        setError(null);
        setMessage(
          `Connected to ${result.account_name ?? "your Stripe account"} (${result.mode} mode)` +
            (result.charges_enabled === false ? " — but this account can't take payments yet; finish Stripe's onboarding." : ".")
        );
      } else {
        setMessage(null);
        setError(result.message ?? "Stripe connection failed.");
      }
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not test the connection."),
  });

  const settings = settingsQuery.data;
  const toCents = (value: string) => Math.round(Number(value) * 100);
  const priceValid = (value: string) => value === "" || (Number.isFinite(Number(value)) && Number(value) >= 0);

  function saveKeys() {
    const payload: Parameters<typeof adminApi.updatePaymentSettings>[0] = {};
    if (secretKey.trim()) payload.stripe_secret_key = secretKey.trim();
    if (webhookSecret.trim()) payload.stripe_webhook_secret = webhookSecret.trim();
    saveMutation.mutate(payload);
  }

  function savePrices() {
    const payload: Parameters<typeof adminApi.updatePaymentSettings>[0] = {};
    if (sessionPrice !== "") payload.session_topup_price_cents = toCents(sessionPrice);
    if (exportPrice !== "") payload.export_topup_price_cents = toCents(exportPrice);
    saveMutation.mutate(payload);
  }

  return (
    <Card>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Stripe payments
        </h2>
        {settings &&
          (settings.stripe_configured ? (
            <Badge tone={settings.stripe_mode === "live" ? "green" : "amber"}>
              {settings.stripe_mode === "live" ? "Live" : "Test mode"} · key …{settings.stripe_key_last4}
            </Badge>
          ) : (
            <Badge tone="red">Not set up — customers can't pay</Badge>
          ))}
      </div>
      <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
        All subscriptions and top-ups are paid through Stripe. Keys are stored encrypted and are never shown
        again after saving — paste a new one to replace it. Use test keys (sk_test_…) to try things out first.
      </p>

      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Secret key (sk_… or restricted rk_…)" htmlFor="stripeSecret">
          <Input
            id="stripeSecret"
            type="password"
            autoComplete="off"
            placeholder={settings?.stripe_configured ? `Saved — ends in ${settings.stripe_key_last4}` : "sk_live_…"}
            value={secretKey}
            onChange={(e) => setSecretKey(e.target.value)}
          />
        </Field>
        <Field label="Webhook signing secret (whsec_…)" htmlFor="stripeWebhook">
          <Input
            id="stripeWebhook"
            type="password"
            autoComplete="off"
            placeholder={settings?.webhook_configured ? "Saved" : "whsec_…"}
            value={webhookSecret}
            onChange={(e) => setWebhookSecret(e.target.value)}
          />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button disabled={(!secretKey.trim() && !webhookSecret.trim()) || saveMutation.isPending} onClick={saveKeys}>
          {saveMutation.isPending ? "Saving…" : "Save keys"}
        </Button>
        <Button
          variant="secondary"
          disabled={!settings?.stripe_configured || testMutation.isPending}
          onClick={() => testMutation.mutate()}
        >
          {testMutation.isPending ? "Testing…" : "Test connection"}
        </Button>
      </div>

      <details className="mt-4 rounded-md border border-slate-200 p-3 text-sm dark:border-slate-700">
        <summary className="cursor-pointer font-medium text-slate-700 dark:text-slate-300">
          Webhook setup (needed so renewals and failed payments update automatically)
        </summary>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-slate-600 dark:text-slate-300">
          <li>In Stripe, open Developers → Webhooks → Add endpoint.</li>
          <li>
            Endpoint URL:{" "}
            <code className="rounded bg-slate-100 px-1 py-0.5 text-xs dark:bg-slate-800">{webhookUrl}</code>
          </li>
          <li>
            Select these events:{" "}
            <span className="text-xs text-slate-500 dark:text-slate-400">{WEBHOOK_EVENTS.join(", ")}</span>
          </li>
          <li>Copy the endpoint's signing secret (whsec_…) into the field above.</li>
          <li>In Settings → Billing → Customer portal, turn the portal on so customers can update their card.</li>
        </ol>
      </details>

      <div className="mt-6 border-t border-slate-100 pt-4 dark:border-slate-700">
        <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-200">Top-up prices</h3>
        <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
          What an organization pays for one extra session or export after using its monthly allowance. Extras never
          expire. Set to 0 to stop selling that extra.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Per extra session (USD)" htmlFor="sessionTopup">
            <Input
              id="sessionTopup"
              type="number"
              min={0}
              step="0.01"
              className="w-40"
              placeholder={settings ? (settings.session_topup_price_cents / 100).toFixed(2) : ""}
              value={sessionPrice}
              onChange={(e) => setSessionPrice(e.target.value)}
            />
          </Field>
          <Field label="Per extra export (USD)" htmlFor="exportTopup">
            <Input
              id="exportTopup"
              type="number"
              min={0}
              step="0.01"
              className="w-40"
              placeholder={settings ? (settings.export_topup_price_cents / 100).toFixed(2) : ""}
              value={exportPrice}
              onChange={(e) => setExportPrice(e.target.value)}
            />
          </Field>
          <Button
            variant="secondary"
            disabled={
              (sessionPrice === "" && exportPrice === "") ||
              !priceValid(sessionPrice) ||
              !priceValid(exportPrice) ||
              saveMutation.isPending
            }
            onClick={savePrices}
          >
            Save prices
          </Button>
        </div>
      </div>
      {message && <p className="mt-3 text-sm text-green-600 dark:text-green-400">{message}</p>}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function PlanPriceRow({ plan, annualSavingsPercent }: { plan: Plan; annualSavingsPercent: number }) {
  const queryClient = useQueryClient();
  const [price, setPrice] = useState("");
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const newCents = price === "" ? null : Math.round(Number(price) * 100);
  const valid = newCents !== null && Number.isFinite(newCents) && newCents >= 0 && newCents !== plan.monthly_price_cents;
  const yearlyCents = Math.round(plan.monthly_price_cents * 12 * (100 - annualSavingsPercent) / 100);

  const mutation = useMutation({
    mutationFn: () => adminApi.updatePlanPrice(plan.id, newCents!, reason.trim()),
    onSuccess: (updated) => {
      setPrice("");
      setReason("");
      setError(null);
      setMessage(
        `Price set to ${formatUsd(updated.plan.monthly_price_cents)} / month. ${updated.owners_notified} owner(s) emailed` +
          (updated.subscriptions_updated || updated.subscriptions_failed
            ? `; ${updated.subscriptions_updated} subscription(s) move to the new price at their next renewal` +
              (updated.subscriptions_failed ? ` (${updated.subscriptions_failed} failed — see the audit log)` : "")
            : "") +
          "."
      );
      queryClient.invalidateQueries({ queryKey: ["plans"] });
    },
    onError: (err) => {
      setMessage(null);
      setError(err instanceof ApiError ? err.message : "Could not update the price.");
    },
  });

  return (
    <div className="space-y-3 border-b border-slate-100 pb-5 last:border-0 last:pb-0 dark:border-slate-700">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-semibold text-slate-900 dark:text-slate-100">{plan.name}</span>
        <span className="text-sm text-slate-600 dark:text-slate-300">
          {formatUsd(plan.monthly_price_cents)} / month
          {plan.monthly_price_cents > 0 && (
            <span className="text-slate-400 dark:text-slate-500"> · {formatUsd(yearlyCents)} / year</span>
          )}
        </span>
      </div>
      <div className="grid gap-3 sm:grid-cols-[13rem_1fr]">
        <Field label="New monthly price (USD)" htmlFor={`price-${plan.id}`}>
          <Input
            id={`price-${plan.id}`}
            type="number"
            min={0}
            step="0.01"
            value={price}
            placeholder={(plan.monthly_price_cents / 100).toFixed(2)}
            onChange={(e) => setPrice(e.target.value)}
          />
        </Field>
        <Field label="Reason (included in the email to owners)" htmlFor={`reason-${plan.id}`}>
          <Textarea
            id={`reason-${plan.id}`}
            rows={2}
            maxLength={2000}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
      </div>
      {message && <p className="text-sm text-green-600 dark:text-green-400">{message}</p>}
      <ErrorText>{error}</ErrorText>
      <Button
        variant="secondary"
        disabled={!valid || reason.trim().length < 3 || mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        {mutation.isPending ? "Saving…" : "Update price and notify owners"}
      </Button>
    </div>
  );
}

function AnnualSavingsCard() {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const settingsQuery = useQuery({ queryKey: ["admin-billing-settings"], queryFn: adminApi.getBillingSettings });

  const mutation = useMutation({
    mutationFn: () => adminApi.updateBillingSettings(Number(value)),
    onSuccess: () => {
      setValue("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["admin-billing-settings"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not save."),
  });

  const numeric = Number(value);
  const valid = value !== "" && Number.isInteger(numeric) && numeric >= 0 && numeric <= 100;

  return (
    <Card>
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Yearly billing
      </h2>
      <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
        Yearly subscriptions cost 12 months of the plan price minus this saving, across every paid plan.
        Currently{" "}
        <span className="font-semibold">
          {settingsQuery.data ? `${settingsQuery.data.annual_savings_percent}%` : "…"}
        </span>
        . Existing yearly subscribers move to the new price at their next renewal.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Yearly saving (%)" htmlFor="annualSavings">
          <Input
            id="annualSavings"
            type="number"
            min={0}
            max={100}
            className="w-32"
            value={value}
            placeholder={settingsQuery.data ? `${settingsQuery.data.annual_savings_percent}` : ""}
            onChange={(e) => setValue(e.target.value)}
          />
        </Field>
        <Button disabled={!valid || mutation.isPending} onClick={() => mutation.mutate()}>
          {mutation.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function PromotionsCard({ plans }: { plans: Plan[] }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [percentOff, setPercentOff] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [endsOn, setEndsOn] = useState("");
  const [planId, setPlanId] = useState("");
  const [forNew, setForNew] = useState(true);
  const [forExisting, setForExisting] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const promotionsQuery = useQuery({ queryKey: ["admin-promotions"], queryFn: adminApi.listPromotions });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["admin-promotions"] });

  const createMutation = useMutation({
    mutationFn: () =>
      adminApi.createPromotion({
        name: name.trim(),
        percent_off: Number(percentOff),
        starts_at: startOfDay(startsOn),
        ends_at: endOfDay(endsOn),
        plan_id: planId || null,
        applies_to_new: forNew,
        applies_to_existing: forExisting,
      }),
    onSuccess: () => {
      setName("");
      setPercentOff("");
      setStartsOn("");
      setEndsOn("");
      setPlanId("");
      setError(null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not create the promotion."),
  });

  const toggleMutation = useMutation({
    mutationFn: (promotion: BillingPromotion) => adminApi.setPromotionActive(promotion.id, !promotion.is_active),
    onSuccess: invalidate,
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not update the promotion."),
  });

  const percent = Number(percentOff);
  const valid =
    name.trim() !== "" &&
    Number.isInteger(percent) &&
    percent >= 1 &&
    percent <= 100 &&
    startsOn !== "" &&
    endsOn !== "" &&
    endsOn >= startsOn &&
    (forNew || forExisting);
  const planName = (id: string | null) => (id ? plans.find((p) => p.id === id)?.name ?? "Unknown plan" : "All paid plans");

  return (
    <Card>
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Promotions
      </h2>
      <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
        Every payment that falls between the start and end dates is charged at the discounted price;
        billing returns to the normal price automatically afterwards. New subscribers get it at checkout;
        existing subscribers see a one-click "Claim offer" on their Billing page. If several promotions
        overlap, the largest discount wins.
      </p>

      <div className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
          <Field label="Name" htmlFor="promoName">
            <Input id="promoName" maxLength={150} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Discount (%)" htmlFor="promoPercent">
            <Input
              id="promoPercent"
              type="number"
              min={1}
              max={100}
              value={percentOff}
              onChange={(e) => setPercentOff(e.target.value)}
            />
          </Field>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Starts" htmlFor="promoStarts">
            <Input id="promoStarts" type="date" value={startsOn} onChange={(e) => setStartsOn(e.target.value)} />
          </Field>
          <Field label="Ends" htmlFor="promoEnds">
            <Input id="promoEnds" type="date" value={endsOn} onChange={(e) => setEndsOn(e.target.value)} />
          </Field>
          <Field label="Plan" htmlFor="promoPlan">
            <Select id="promoPlan" value={planId} onChange={(e) => setPlanId(e.target.value)}>
              <option value="">All paid plans</option>
              {plans
                .filter((p) => p.monthly_price_cents > 0)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
            </Select>
          </Field>
        </div>
        <div className="flex flex-wrap gap-5 text-sm text-slate-700 dark:text-slate-300">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={forNew} onChange={(e) => setForNew(e.target.checked)} />
            New subscriptions
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={forExisting} onChange={(e) => setForExisting(e.target.checked)} />
            Existing subscriptions
          </label>
        </div>
        <ErrorText>{error}</ErrorText>
        <Button disabled={!valid || createMutation.isPending} onClick={() => createMutation.mutate()}>
          {createMutation.isPending ? "Creating…" : "Create promotion"}
        </Button>
      </div>

      <div className="mt-6 space-y-2 border-t border-slate-100 pt-4 dark:border-slate-700">
        {promotionsQuery.isLoading ? (
          <Spinner className="h-5 w-5 text-brand-600" />
        ) : promotionsQuery.data?.length ? (
          promotionsQuery.data.map((promotion) => {
            const state = promotionState(promotion);
            return (
              <div
                key={promotion.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-slate-200 px-3 py-2 text-sm dark:border-slate-700"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-slate-900 dark:text-slate-100">{promotion.name}</span>
                    <Badge tone={state.tone}>{state.label}</Badge>
                  </div>
                  <div className="text-xs text-slate-500 dark:text-slate-400">
                    {promotion.percent_off}% off · {planName(promotion.plan_id)} ·{" "}
                    {new Date(promotion.starts_at).toLocaleDateString()} –{" "}
                    {new Date(promotion.ends_at).toLocaleDateString()} ·{" "}
                    {[promotion.applies_to_new && "new", promotion.applies_to_existing && "existing"]
                      .filter(Boolean)
                      .join(" + ")}{" "}
                    subscribers
                  </div>
                </div>
                <Button
                  variant="secondary"
                  disabled={toggleMutation.isPending}
                  onClick={() => toggleMutation.mutate(promotion)}
                >
                  {promotion.is_active ? "Disable" : "Enable"}
                </Button>
              </div>
            );
          })
        ) : (
          <p className="text-sm text-slate-500 dark:text-slate-400">No promotions yet.</p>
        )}
      </div>
    </Card>
  );
}

export default function AdminPricingPage() {
  const plansQuery = useQuery({ queryKey: ["plans"], queryFn: plansApi.list });
  const settingsQuery = useQuery({ queryKey: ["admin-billing-settings"], queryFn: adminApi.getBillingSettings });

  if (plansQuery.isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }
  const plans = plansQuery.data ?? [];

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Pricing &amp; payments</h1>

      <StripeSettingsCard />

      <Card>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Plan prices
        </h2>
        <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
          New checkouts use the new price immediately. Every active owner on the plan is emailed the
          change with your reason, and existing subscribers move to the new price from their next
          renewal (nothing is charged early).
        </p>
        <div className="space-y-5">
          {plans.map((plan) => (
            <PlanPriceRow
              key={plan.id}
              plan={plan}
              annualSavingsPercent={settingsQuery.data?.annual_savings_percent ?? 0}
            />
          ))}
        </div>
      </Card>

      <AnnualSavingsCard />
      <PromotionsCard plans={plans} />
    </div>
  );
}
