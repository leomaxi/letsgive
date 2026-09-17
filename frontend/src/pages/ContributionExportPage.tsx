import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError, getToken } from "@/lib/api";
import { connectionApi, reportApi } from "@/lib/endpoints";
import type { ApiErrorBody, ContributionExportField } from "@/lib/types";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Input, PageHeader, Select, Spinner } from "@/components/ui";

const DEFAULT_FIELD_KEYS = ["sender_name", "received_at", "amount", "message", "reference_number"];

function localDateTimeValue(date: Date): string {
  const offsetMs = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
}

function toIsoFromLocal(value: string): string {
  return new Date(value).toISOString();
}

async function downloadCsv(url: string, filename: string, setError: (value: string | null) => void) {
  setError(null);
  try {
    const token = getToken();
    const response = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
      let detail = `Export failed (${response.status})`;
      if (response.headers.get("content-type")?.includes("application/json")) {
        const body = (await response.json()) as ApiErrorBody;
        if (typeof body.detail === "string") detail = body.detail;
      }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);
  } catch (err) {
    setError(err instanceof Error ? err.message : "Could not download the export.");
  }
}

export default function ContributionExportPage() {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();
  const nowDefaults = useMemo(() => {
    const now = new Date();
    const weekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
    return { from: localDateTimeValue(weekAgo), to: localDateTimeValue(now) };
  }, []);

  const [fromDateTime, setFromDateTime] = useState(nowDefaults.from);
  const [toDateTime, setToDateTime] = useState(nowDefaults.to);
  const [keywords, setKeywords] = useState("");
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [sampleConnectionId, setSampleConnectionId] = useState("");
  const [selectedFields, setSelectedFields] = useState<string[]>(DEFAULT_FIELD_KEYS);
  const [templateName, setTemplateName] = useState("");
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [templateError, setTemplateError] = useState<string | null>(null);

  const canUse = activeOrg?.role === "owner" || activeOrg?.role === "finance";

  const connectionsQuery = useQuery({
    queryKey: ["connections", activeOrg?.id],
    queryFn: () => connectionApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg && canUse,
  });

  const templatesQuery = useQuery({
    queryKey: ["contribution-export-templates", activeOrg?.id],
    queryFn: () => reportApi.listContributionExportTemplates(activeOrg!.id),
    enabled: !!activeOrg && canUse,
  });

  const fieldsQuery = useQuery({
    queryKey: ["contribution-export-fields", activeOrg?.id, sampleConnectionId],
    queryFn: () => reportApi.sampleContributionExportFields(activeOrg!.id, sampleConnectionId || undefined),
    enabled: false,
  });

  const createTemplateMutation = useMutation({
    mutationFn: () =>
      reportApi.createContributionExportTemplate(activeOrg!.id, {
        name: templateName,
        field_keys: selectedFields,
        field_labels: Object.fromEntries((fieldsQuery.data ?? []).map((field) => [field.key, field.label])),
        sample: {
          source_connection_id: sampleConnectionId || null,
          field_keys: selectedFields,
        },
      }),
    onSuccess: (template) => {
      setTemplateError(null);
      setTemplateName("");
      setSelectedTemplateId(template.id);
      queryClient.invalidateQueries({ queryKey: ["contribution-export-templates", activeOrg?.id] });
    },
    onError: (err) => setTemplateError(err instanceof ApiError ? err.message : "Could not save the template."),
  });

  const deleteTemplateMutation = useMutation({
    mutationFn: (templateId: string) => reportApi.deleteContributionExportTemplate(activeOrg!.id, templateId),
    onSuccess: (_, deletedId) => {
      if (selectedTemplateId === deletedId) setSelectedTemplateId("");
      queryClient.invalidateQueries({ queryKey: ["contribution-export-templates", activeOrg?.id] });
    },
    onError: (err) => setTemplateError(err instanceof ApiError ? err.message : "Could not delete the template."),
  });

  if (!activeOrg) return null;

  if (!canUse) {
    return <Card className="text-sm text-slate-500 dark:text-slate-400">Only Owner and Finance can export contribution records.</Card>;
  }

  const templates = templatesQuery.data ?? [];
  const fields: ContributionExportField[] = fieldsQuery.data ?? [];
  const connectedConnections = (connectionsQuery.data ?? []).filter((connection) => connection.status === "connected");

  function toggleField(key: string, checked: boolean) {
    setSelectedFields((current) =>
      checked ? [...new Set([...current, key])] : current.filter((fieldKey) => fieldKey !== key),
    );
  }

  function onExport() {
    if (!activeOrg) return;
    downloadCsv(
      reportApi.contributionExportUrl(activeOrg.id, {
        from_datetime: toIsoFromLocal(fromDateTime),
        to_datetime: toIsoFromLocal(toDateTime),
        keywords: keywords.trim() || undefined,
        template_id: selectedTemplateId || undefined,
      }),
      "contribution-export.csv",
      setDownloadError,
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Contribution export"
        description="Export accepted contribution records for accounting and record keeping."
      />

      <div className="grid gap-6 lg:grid-cols-[1.15fr_0.85fr]">
        <Card>
          <h2 className="mb-4 text-sm font-semibold uppercase text-slate-500 dark:text-slate-400">
            Export CSV
          </h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="From date/time" htmlFor="exportFrom">
              <Input
                id="exportFrom"
                type="datetime-local"
                value={fromDateTime}
                onChange={(e) => setFromDateTime(e.target.value)}
              />
            </Field>
            <Field label="To date/time" htmlFor="exportTo">
              <Input
                id="exportTo"
                type="datetime-local"
                value={toDateTime}
                onChange={(e) => setToDateTime(e.target.value)}
              />
            </Field>
          </div>
          <div className="mt-3 space-y-3">
            <Field label="Comment keywords (comma-separated, optional)" htmlFor="exportKeywords">
              <Input
                id="exportKeywords"
                placeholder="tithe, building fund, missions"
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
              />
            </Field>
            <Field label="Export template" htmlFor="exportTemplate">
              <Select
                id="exportTemplate"
                value={selectedTemplateId}
                onChange={(e) => setSelectedTemplateId(e.target.value)}
              >
                <option value="">Default accounting columns</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.name}
                  </option>
                ))}
              </Select>
            </Field>
            <ErrorText>{downloadError}</ErrorText>
            <Button onClick={onExport} disabled={!fromDateTime || !toDateTime}>
              Download CSV
            </Button>
          </div>
        </Card>

        <Card>
          <h2 className="mb-4 text-sm font-semibold uppercase text-slate-500 dark:text-slate-400">
            Saved templates
          </h2>
          {templatesQuery.isLoading ? (
            <Spinner className="h-5 w-5 text-brand-600" />
          ) : templates.length > 0 ? (
            <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-700">
              {templates.map((template) => (
                <li key={template.id} className="flex items-start justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <div className="font-medium text-slate-700 dark:text-slate-300">{template.name}</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {template.field_keys.map((key) => (
                        <Badge key={key} tone="slate">
                          {template.field_labels[key] ?? key}
                        </Badge>
                      ))}
                    </div>
                  </div>
                  <Button
                    variant="secondary"
                    disabled={deleteTemplateMutation.isPending}
                    onClick={() => deleteTemplateMutation.mutate(template.id)}
                  >
                    Delete
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="No templates yet" description="Populate fields, tick columns, then save a reusable template." />
          )}
          <ErrorText>{templateError}</ErrorText>
        </Card>
      </div>

      <Card>
        <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div>
            <h2 className="text-sm font-semibold uppercase text-slate-500 dark:text-slate-400">
              Populate export fields
            </h2>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              Sample recent deposit messages, then choose the columns to save as a template.
            </p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <Select value={sampleConnectionId} onChange={(e) => setSampleConnectionId(e.target.value)}>
              <option value="">All connected mailboxes</option>
              {connectedConnections.map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.mailbox}
                </option>
              ))}
            </Select>
            <Button variant="secondary" disabled={fieldsQuery.isFetching} onClick={() => fieldsQuery.refetch()}>
              {fieldsQuery.isFetching ? "Populating..." : "Populate export fields"}
            </Button>
          </div>
        </div>

        {fieldsQuery.isFetching ? (
          <Spinner className="h-5 w-5 text-brand-600" />
        ) : fields.length > 0 ? (
          <div className="grid gap-3 md:grid-cols-2">
            {fields.map((field) => (
              <label
                key={field.key}
                className="flex gap-3 rounded-md border border-slate-200 p-3 text-sm dark:border-slate-700"
              >
                <input
                  type="checkbox"
                  className="mt-1 rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
                  checked={selectedFields.includes(field.key)}
                  onChange={(e) => toggleField(field.key, e.target.checked)}
                />
                <span className="min-w-0">
                  <span className="block font-medium text-slate-700 dark:text-slate-300">{field.label}</span>
                  <span className="mt-1 block truncate text-xs text-slate-500 dark:text-slate-400">
                    {field.sample_values[0] || "No sample value found yet"}
                  </span>
                </span>
              </label>
            ))}
          </div>
        ) : (
          <EmptyState title="No sampled fields" description="Use Populate export fields after a mailbox has fetched deposit messages." />
        )}

        <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <Field label="Template name" htmlFor="templateName">
            <Input
              id="templateName"
              placeholder="Monthly accounting export"
              value={templateName}
              onChange={(e) => setTemplateName(e.target.value)}
            />
          </Field>
          <Button
            disabled={createTemplateMutation.isPending || !templateName || selectedFields.length === 0}
            onClick={() => createTemplateMutation.mutate()}
          >
            {createTemplateMutation.isPending ? "Saving..." : "Save as template"}
          </Button>
        </div>
      </Card>
    </div>
  );
}
