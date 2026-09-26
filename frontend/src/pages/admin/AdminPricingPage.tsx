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
      setMessage(`Price set to ${formatUsd(updated.monthly_price_cents)} / month. Owners on this plan were emailed.`);
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
        . Existing yearly subscribers are asked to approve the new price on their Billing page.
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
        existing subscribers approve it once from their Billing page (PayPal requires their consent). If
        several promotions overlap, the largest discount wins.
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
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Pricing</h1>

      <Card>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Plan prices
        </h2>
        <p className="mb-4 text-sm text-slate-600 dark:text-slate-300">
          New checkouts use the new price immediately. Every active owner on the plan is emailed the
          change with your reason. Existing PayPal subscribers keep their current price until they
          approve the new one from their Billing page.
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
