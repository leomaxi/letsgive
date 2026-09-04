import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { displayTemplateApi } from "@/lib/endpoints";
import { TEMPLATE_PRESETS, type TemplatePreset } from "@/lib/templatePresets";
import { Badge, Button, Card, ErrorText, Spinner } from "@/components/ui";
import TemplateCanvasPreview from "@/components/TemplateCanvasPreview";

export default function DisplayStudioListPage() {
  const { activeOrg } = useOrg();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);

  const canEdit = activeOrg?.role === "owner" || activeOrg?.role === "media";

  const query = useQuery({
    queryKey: ["display-templates", activeOrg?.id],
    queryFn: () => displayTemplateApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const createMutation = useMutation({
    mutationFn: () => displayTemplateApi.create(activeOrg!.id),
    onSuccess: (template) => navigate(`/display-studio/${template.id}`),
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not create a template."),
  });

  const createFromPresetMutation = useMutation({
    mutationFn: async (preset: TemplatePreset) => {
      const created = await displayTemplateApi.create(activeOrg!.id);
      return displayTemplateApi.save(activeOrg!.id, created.id, {
        name: preset.label,
        canvas: preset.canvas,
        is_default: created.is_default,
        expected_version: created.version,
        elements: preset.elements,
      });
    },
    onSuccess: (template) => navigate(`/display-studio/${template.id}`),
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not create a template."),
  });

  const duplicateMutation = useMutation({
    mutationFn: (templateId: string) => displayTemplateApi.duplicate(activeOrg!.id, templateId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["display-templates", activeOrg?.id] }),
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not duplicate the template."),
  });

  const deleteMutation = useMutation({
    mutationFn: (templateId: string) => displayTemplateApi.remove(activeOrg!.id, templateId),
    onSuccess: () => {
      setConfirmingDeleteId(null);
      queryClient.invalidateQueries({ queryKey: ["display-templates", activeOrg?.id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not delete the template."),
  });

  if (!activeOrg) return null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Display Studio</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Layouts for the public projection screen. The default template is bound to new sessions
            automatically.
          </p>
        </div>
        {canEdit && (
          <Button
            disabled={createMutation.isPending || createFromPresetMutation.isPending}
            onClick={() => setPickerOpen((v) => !v)}
          >
            New template
          </Button>
        )}
      </div>

      <ErrorText>{error}</ErrorText>

      {canEdit && pickerOpen && (
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Start from
            </h2>
            <Button
              variant="secondary"
              disabled={createMutation.isPending}
              onClick={() => {
                setPickerOpen(false);
                createMutation.mutate();
              }}
            >
              {createMutation.isPending ? "Creating…" : "Blank template"}
            </Button>
          </div>
          <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {TEMPLATE_PRESETS.map((preset) => (
              <button
                key={preset.key}
                disabled={createFromPresetMutation.isPending}
                onClick={() => {
                  setPickerOpen(false);
                  createFromPresetMutation.mutate(preset);
                }}
                className="flex flex-col rounded-lg border border-slate-200 p-3 text-left transition hover:border-brand-300 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700"
              >
                <TemplateCanvasPreview canvas={preset.canvas} elements={preset.elements} />
                <span className="mt-2 font-medium text-slate-900 dark:text-slate-100">{preset.label}</span>
                <span className="mt-1 text-xs text-slate-500 dark:text-slate-400">{preset.description}</span>
              </button>
            ))}
          </div>
        </Card>
      )}

      {query.isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      ) : query.data && query.data.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {query.data.map((template) => (
            <Card key={template.id} className="cursor-pointer hover:border-brand-300">
              <div onClick={() => navigate(`/display-studio/${template.id}`)}>
                <div className="flex items-start justify-between">
                  <span className="font-semibold text-slate-900 dark:text-slate-100">{template.name}</span>
                  {template.is_default && <Badge tone="blue">Default</Badge>}
                </div>
                <TemplateCanvasPreview
                  canvas={template.canvas}
                  elements={template.elements}
                  className="mt-3"
                />
              </div>
              {canEdit && (
                <div className="mt-3 flex gap-2 border-t border-slate-100 pt-3 dark:border-slate-700">
                  <Button
                    variant="secondary"
                    disabled={duplicateMutation.isPending}
                    onClick={() => duplicateMutation.mutate(template.id)}
                  >
                    Duplicate
                  </Button>
                  {confirmingDeleteId === template.id ? (
                    <>
                      <Button
                        variant="danger"
                        disabled={deleteMutation.isPending}
                        onClick={() => deleteMutation.mutate(template.id)}
                      >
                        Confirm delete
                      </Button>
                      <Button variant="secondary" onClick={() => setConfirmingDeleteId(null)}>
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <Button variant="secondary" onClick={() => setConfirmingDeleteId(template.id)}>
                      Delete
                    </Button>
                  )}
                </div>
              )}
            </Card>
          ))}
        </div>
      ) : (
        <Card className="text-sm text-slate-500 dark:text-slate-400">
          No templates yet.{" "}
          {canEdit
            ? "Create one to start designing the projection screen."
            : "Ask an Owner or Media teammate to create one."}
        </Card>
      )}
    </div>
  );
}
