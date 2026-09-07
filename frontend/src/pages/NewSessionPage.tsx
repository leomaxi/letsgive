import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { connectionApi, displayTemplateApi, sessionApi } from "@/lib/endpoints";
import { Button, Card, ErrorText, Field, Input, Label } from "@/components/ui";

const CONTRIBUTION_METHODS = ["e-transfer", "bank-transfer", "mobile-money", "other"];
const MAX_DURATION_SECONDS = 6 * 60 * 60;

function toLocalDatetimeInputValue(date: Date): string {
  // datetime-local inputs want "YYYY-MM-DDTHH:mm" in the *local* timezone --
  // toISOString() is UTC, so build it from the local getters instead.
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatDuration(totalSeconds: number): string {
  if (totalSeconds <= 0) return "";
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.round((totalSeconds % 3600) / 60);
  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0 || hours === 0) parts.push(`${minutes}m`);
  return parts.join(" ");
}

export default function NewSessionPage() {
  const { activeOrg } = useOrg();
  const navigate = useNavigate();

  const connectionsQuery = useQuery({
    queryKey: ["connections", activeOrg?.id],
    queryFn: () => connectionApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const templatesQuery = useQuery({
    queryKey: ["display-templates", activeOrg?.id],
    queryFn: () => displayTemplateApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const [contributionMethod, setContributionMethod] = useState(CONTRIBUTION_METHODS[0]);
  const [startAt, setStartAt] = useState(() => toLocalDatetimeInputValue(new Date()));
  const [endAt, setEndAt] = useState(() =>
    toLocalDatetimeInputValue(new Date(Date.now() + 30 * 60 * 1000))
  );
  const [connectionId, setConnectionId] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [testMode, setTestMode] = useState(false);
  const [goalEnabled, setGoalEnabled] = useState(false);
  const [goalAmount, setGoalAmount] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const durationSeconds = Math.round(
    (new Date(endAt).getTime() - new Date(startAt).getTime()) / 1000
  );
  const durationError =
    !startAt || !endAt
      ? null
      : durationSeconds <= 0
        ? "End must be after start."
        : durationSeconds > MAX_DURATION_SECONDS
          ? "Sessions can't run longer than 6 hours."
          : null;

  if (!activeOrg) return null;

  if (activeOrg.role !== "owner" && activeOrg.role !== "media" && activeOrg.role !== "finance") {
    return (
      <Card className="mx-auto max-w-lg text-sm text-slate-500 dark:text-slate-400">
        Only Media and Finance can create sessions.
      </Card>
    );
  }

  const connectedConnections = connectionsQuery.data?.filter((c) => c.status === "connected") ?? [];

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (durationError) {
      setError(durationError);
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const session = await sessionApi.create({
        organization_id: activeOrg!.id,
        contribution_method: contributionMethod,
        duration_seconds: durationSeconds,
        mailbox_connection_id: connectionId || undefined,
        display_template_id: templateId || undefined,
        test_mode: testMode,
        goal_enabled: goalEnabled,
        goal_amount: goalEnabled && goalAmount ? goalAmount : undefined,
      });
      navigate(`/sessions/${session.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the session.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-lg">
      <Card>
        <h1 className="mb-1 text-xl font-semibold text-slate-900 dark:text-slate-100">New session</h1>
        <p className="mb-6 text-sm text-slate-500 dark:text-slate-400">
          You'll send a finance approval code before monitoring can begin.
        </p>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div>
            <Label htmlFor="method">Contribution method</Label>
            <select
              id="method"
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              value={contributionMethod}
              onChange={(e) => setContributionMethod(e.target.value)}
            >
              {CONTRIBUTION_METHODS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Start" htmlFor="startAt">
              <Input
                id="startAt"
                type="datetime-local"
                required
                value={startAt}
                onChange={(e) => setStartAt(e.target.value)}
              />
            </Field>
            <Field label="End" htmlFor="endAt">
              <Input
                id="endAt"
                type="datetime-local"
                required
                value={endAt}
                onChange={(e) => setEndAt(e.target.value)}
              />
            </Field>
          </div>
          <p className={`text-xs ${durationError ? "text-red-600 dark:text-red-400" : "text-slate-500 dark:text-slate-400"}`}>
            {durationError ?? (durationSeconds > 0 ? `Duration: ${formatDuration(durationSeconds)}` : "")}
          </p>

          {connectedConnections.length > 0 && (
            <div>
              <Label htmlFor="connection">Mailbox connection</Label>
              <select
                id="connection"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
                value={connectionId}
                onChange={(e) => setConnectionId(e.target.value)}
              >
                <option value="">
                  {connectedConnections.length === 1 ? "Auto (only connection)" : "Select…"}
                </option>
                {connectedConnections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.mailbox}
                  </option>
                ))}
              </select>
            </div>
          )}
          {connectedConnections.length === 0 && !connectionsQuery.isLoading && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              No connected mailbox yet — this session won't receive live deposit notifications
              until one is connected. You can still use test mode below.
            </p>
          )}

          {templatesQuery.data && templatesQuery.data.length > 0 && (
            <div>
              <Label htmlFor="template">Display template</Label>
              <select
                id="template"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
                value={templateId}
                onChange={(e) => setTemplateId(e.target.value)}
              >
                <option value="">
                  {templatesQuery.data.some((t) => t.is_default) ? "Default template" : "None"}
                </option>
                {templatesQuery.data.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                    {t.is_default ? " (default)" : ""}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
              <input
                type="checkbox"
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
                checked={goalEnabled}
                onChange={(e) => setGoalEnabled(e.target.checked)}
              />
              Set a fundraising target (optional)
            </label>
            {goalEnabled && (
              <div className="mt-2">
                <Input
                  type="number"
                  min={0}
                  step="0.01"
                  placeholder="Target amount"
                  value={goalAmount}
                  onChange={(e) => setGoalAmount(e.target.value)}
                />
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  Shown on the projection screen as a progress bar; a celebration appears once it's
                  reached.
                </p>
              </div>
            )}
          </div>

          <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
            <input
              type="checkbox"
              className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
              checked={testMode}
              onChange={(e) => setTestMode(e.target.checked)}
            />
            Test mode (rehearse the display with synthetic deposits)
          </label>

          <ErrorText>{error}</ErrorText>
          <Button type="submit" className="w-full" disabled={submitting || !!durationError}>
            {submitting ? "Creating…" : "Create session"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
