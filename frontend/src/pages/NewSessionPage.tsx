import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { connectionApi, displayTemplateApi, sessionApi } from "@/lib/endpoints";
import { Button, Card, ErrorText, Field, Input, Label } from "@/components/ui";

const CONTRIBUTION_METHODS = ["e-transfer", "bank-transfer", "mobile-money", "other"];

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
  const [durationMinutes, setDurationMinutes] = useState(30);
  const [connectionId, setConnectionId] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [testMode, setTestMode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!activeOrg) return null;

  if (activeOrg.role !== "media" && activeOrg.role !== "finance") {
    return (
      <Card className="mx-auto max-w-lg text-sm text-slate-500 dark:text-slate-400">
        Only Media and Finance can create sessions.
      </Card>
    );
  }

  const connectedConnections = connectionsQuery.data?.filter((c) => c.status === "connected") ?? [];

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const session = await sessionApi.create({
        organization_id: activeOrg!.id,
        contribution_method: contributionMethod,
        duration_seconds: durationMinutes * 60,
        mailbox_connection_id: connectionId || undefined,
        display_template_id: templateId || undefined,
        test_mode: testMode,
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

          <Field label="Duration (minutes)" htmlFor="duration">
            <Input
              id="duration"
              type="number"
              min={1}
              max={360}
              required
              value={durationMinutes}
              onChange={(e) => setDurationMinutes(Number(e.target.value))}
            />
          </Field>

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
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Creating…" : "Create session"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
