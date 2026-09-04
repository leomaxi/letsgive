import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import QRCode from "qrcode";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { displayTemplateApi } from "@/lib/endpoints";
import type { DisplayElement, ElementType } from "@/lib/types";
import { ELEMENT_LABELS, previewText } from "@/lib/templateElementStyle";
import { Badge, Button, Card, ErrorText, Field, Input, Spinner } from "@/components/ui";

const CANVAS_RENDER_WIDTH = 800;

const ALL_ELEMENT_TYPES = Object.keys(ELEMENT_LABELS) as ElementType[];
const TEXT_BINDING_TYPES = new Set<ElementType>([
  "heading",
  "body_text",
  "sponsor_message",
  "payment_instructions",
]);

function newElement(type: ElementType, zIndex: number): DisplayElement {
  return {
    id: `local-${crypto.randomUUID()}`,
    type,
    x: 80,
    y: 80,
    width: type === "background" ? 1920 : 400,
    height: type === "background" ? 1080 : 150,
    z_index: zIndex,
    style: {},
    binding: TEXT_BINDING_TYPES.has(type) ? { text: ELEMENT_LABELS[type] } : {},
    is_locked: false,
    is_hidden: false,
  };
}

function QrPreview({ value }: { value: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (canvasRef.current) {
      QRCode.toCanvas(canvasRef.current, value, { width: 256, margin: 1 }).catch(() => {
        /* invalid/empty value -- canvas just stays blank */
      });
    }
  }, [value]);

  return <canvas ref={canvasRef} className="h-full max-h-full w-full max-w-full object-contain" />;
}

export default function DisplayStudioEditorPage() {
  const { templateId } = useParams<{ templateId: string }>();
  const { activeOrg } = useOrg();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const canEdit = activeOrg?.role === "owner" || activeOrg?.role === "media";

  const query = useQuery({
    queryKey: ["display-template", activeOrg?.id, templateId],
    queryFn: () => displayTemplateApi.get(activeOrg!.id, templateId!),
    enabled: !!activeOrg && !!templateId,
  });

  const [name, setName] = useState("");
  const [backgroundColor, setBackgroundColor] = useState("#0b1220");
  const [canvasSize, setCanvasSize] = useState({ width: 1920, height: 1080 });
  const [isDefault, setIsDefault] = useState(false);
  const [expectedVersion, setExpectedVersion] = useState(1);
  const [elements, setElements] = useState<DisplayElement[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  // Reset the "has this template's data been pulled into local edit state
  // yet" flag whenever the route's templateId changes (duplicate navigates
  // here, or the user picks a different template without a full remount).
  useEffect(() => {
    setLoaded(false);
    setSelectedId(null);
  }, [templateId]);

  useEffect(() => {
    if (query.data && !loaded) {
      const t = query.data;
      setName(t.name);
      setBackgroundColor(t.canvas.background_color);
      setCanvasSize({ width: t.canvas.width, height: t.canvas.height });
      setIsDefault(t.is_default);
      setExpectedVersion(t.version);
      setElements(t.elements);
      setLoaded(true);
    }
  }, [query.data, loaded]);

  const saveMutation = useMutation({
    mutationFn: () =>
      displayTemplateApi.save(activeOrg!.id, templateId!, {
        name,
        canvas: { width: canvasSize.width, height: canvasSize.height, background_color: backgroundColor },
        is_default: isDefault,
        expected_version: expectedVersion,
        elements: elements.map((el) => ({
          type: el.type,
          x: el.x,
          y: el.y,
          width: el.width,
          height: el.height,
          z_index: el.z_index,
          style: el.style,
          binding: el.binding,
          is_locked: el.is_locked,
          is_hidden: el.is_hidden,
        })),
      }),
    onSuccess: (saved) => {
      setError(null);
      setConflict(false);
      setExpectedVersion(saved.version);
      setElements(saved.elements);
      setIsDefault(saved.is_default);
      queryClient.setQueryData(["display-template", activeOrg?.id, templateId], saved);
      queryClient.invalidateQueries({ queryKey: ["display-templates", activeOrg?.id] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setConflict(true);
        setError(err.message);
      } else {
        setError(err instanceof ApiError ? err.message : "Could not save the template.");
      }
    },
  });

  const duplicateMutation = useMutation({
    mutationFn: () => displayTemplateApi.duplicate(activeOrg!.id, templateId!),
    onSuccess: (copy) => {
      queryClient.invalidateQueries({ queryKey: ["display-templates", activeOrg?.id] });
      navigate(`/display-studio/${copy.id}`);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not duplicate the template."),
  });

  const deleteMutation = useMutation({
    mutationFn: () => displayTemplateApi.remove(activeOrg!.id, templateId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["display-templates", activeOrg?.id] });
      navigate("/display-studio");
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not delete the template."),
  });

  async function reloadAfterConflict() {
    setConflict(false);
    setError(null);
    setLoaded(false);
    await query.refetch();
  }

  function updateSelected(patch: Partial<DisplayElement>) {
    if (!selectedId) return;
    setElements((prev) => prev.map((el) => (el.id === selectedId ? { ...el, ...patch } : el)));
  }

  function startDrag(e: React.PointerEvent, elementId: string, mode: "move" | "resize") {
    if (!canEdit) return;
    const el = elements.find((x) => x.id === elementId);
    if (!el || el.is_locked) return;
    e.stopPropagation();
    setSelectedId(elementId);

    const startX = e.clientX;
    const startY = e.clientY;
    const { x: origX, y: origY, width: origW, height: origH } = el;
    const s = scale;

    function onMove(ev: PointerEvent) {
      const dx = (ev.clientX - startX) / s;
      const dy = (ev.clientY - startY) / s;
      setElements((prev) =>
        prev.map((it) => {
          if (it.id !== elementId) return it;
          if (mode === "move") {
            return { ...it, x: Math.max(0, Math.round(origX + dx)), y: Math.max(0, Math.round(origY + dy)) };
          }
          return {
            ...it,
            width: Math.max(20, Math.round(origW + dx)),
            height: Math.max(20, Math.round(origH + dy)),
          };
        })
      );
    }
    function onUp() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  if (!activeOrg) return null;

  if (query.isLoading || !loaded) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }

  if (!query.data) {
    return <Card className="text-sm text-slate-500 dark:text-slate-400">Template not found.</Card>;
  }

  const scale = CANVAS_RENDER_WIDTH / canvasSize.width;
  const renderHeight = canvasSize.height * scale;
  const selected = elements.find((el) => el.id === selectedId) ?? null;
  const sortedForLayers = [...elements].sort((a, b) => b.z_index - a.z_index);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link to="/display-studio" className="text-sm text-brand-600 hover:text-brand-700">
            &larr; Templates
          </Link>
          {canEdit ? (
            <Input value={name} onChange={(e) => setName(e.target.value)} className="w-64" />
          ) : (
            <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-100">{name}</h1>
          )}
          {isDefault && <Badge tone="blue">Default</Badge>}
        </div>

        {canEdit && (
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
              <input
                type="checkbox"
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
                checked={isDefault}
                onChange={(e) => setIsDefault(e.target.checked)}
              />
              Default template
            </label>
            <input
              type="color"
              value={backgroundColor}
              onChange={(e) => setBackgroundColor(e.target.value)}
              title="Canvas background"
              className="h-8 w-8 cursor-pointer rounded border border-slate-300 dark:border-slate-600"
            />
            <Button variant="secondary" disabled={duplicateMutation.isPending} onClick={() => duplicateMutation.mutate()}>
              Duplicate
            </Button>
            {confirmingDelete ? (
              <>
                <Button variant="danger" disabled={deleteMutation.isPending} onClick={() => deleteMutation.mutate()}>
                  Confirm delete
                </Button>
                <Button variant="secondary" onClick={() => setConfirmingDelete(false)}>
                  Cancel
                </Button>
              </>
            ) : (
              <Button variant="secondary" onClick={() => setConfirmingDelete(true)}>
                Delete
              </Button>
            )}
            <Button disabled={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
              {saveMutation.isPending ? "Saving…" : "Save"}
            </Button>
          </div>
        )}
      </div>

      {conflict ? (
        <div className="flex items-center justify-between rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300">
          <span>{error}</span>
          <Button variant="secondary" onClick={reloadAfterConflict}>
            Reload
          </Button>
        </div>
      ) : (
        <ErrorText>{error}</ErrorText>
      )}

      <div className="grid gap-4 lg:grid-cols-[180px_1fr_280px]">
        {canEdit ? (
          <Card>
            <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Add element
            </h3>
            <div className="flex flex-col gap-2">
              {ALL_ELEMENT_TYPES.map((type) => (
                <Button
                  key={type}
                  variant="secondary"
                  onClick={() => {
                    const el = newElement(type, elements.length);
                    setElements((prev) => [...prev, el]);
                    setSelectedId(el.id);
                  }}
                >
                  {ELEMENT_LABELS[type]}
                </Button>
              ))}
            </div>
          </Card>
        ) : (
          <div />
        )}

        <div className="flex flex-col items-center gap-2">
          <div
            className="relative overflow-hidden rounded-lg border border-slate-300 shadow-sm dark:border-slate-600"
            style={{ width: CANVAS_RENDER_WIDTH, height: renderHeight, backgroundColor }}
            onClick={(e) => {
              if (e.target === e.currentTarget) setSelectedId(null);
            }}
          >
            {[...elements]
              .sort((a, b) => a.z_index - b.z_index)
              .map((el) => {
                const align = (el.style.text_align as string) || "center";
                const justify = align === "left" ? "flex-start" : align === "right" ? "flex-end" : "center";
                const fontSize = (typeof el.style.font_size === "number" ? el.style.font_size : 32) * scale;
                return (
                  <div
                    key={el.id}
                    onPointerDown={(e) => {
                      if (!canEdit) {
                        e.stopPropagation();
                        setSelectedId(el.id);
                        return;
                      }
                      startDrag(e, el.id, "move");
                    }}
                    className={`absolute select-none overflow-hidden truncate ${
                      selectedId === el.id ? "ring-2 ring-brand-500" : "ring-1 ring-white/10"
                    } ${canEdit && !el.is_locked ? "cursor-move" : "cursor-pointer"}`}
                    style={{
                      left: el.x * scale,
                      top: el.y * scale,
                      width: el.width * scale,
                      height: el.height * scale,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: justify,
                      textAlign: align as "left" | "right" | "center",
                      color: (el.style.color as string) || "#f5f7fa",
                      backgroundColor:
                        (el.style.background_color as string) ||
                        (el.type === "background" ? "rgba(255,255,255,0.06)" : "transparent"),
                      fontSize,
                      opacity: el.is_hidden ? 0.3 : typeof el.style.opacity === "number" ? el.style.opacity : 1,
                      border: el.type !== "background" ? "1px dashed rgba(255,255,255,0.25)" : undefined,
                      padding: 4,
                    }}
                  >
                    {el.type === "logo" && el.binding.image_url ? (
                      <img
                        src={el.binding.image_url as string}
                        alt=""
                        className="pointer-events-none h-full max-h-full w-full max-w-full object-contain"
                      />
                    ) : el.type === "qr_code" && el.binding.value ? (
                      <div className="pointer-events-none flex h-full w-full items-center justify-center bg-white p-1">
                        <QrPreview value={el.binding.value as string} />
                      </div>
                    ) : (
                      <span className="truncate">{previewText(el)}</span>
                    )}
                    {canEdit && !el.is_locked && selectedId === el.id && (
                      <div
                        onPointerDown={(e) => startDrag(e, el.id, "resize")}
                        className="absolute -bottom-1 -right-1 h-3 w-3 cursor-se-resize rounded-sm bg-brand-500"
                      />
                    )}
                  </div>
                );
              })}
          </div>
          <p className="text-xs text-slate-400 dark:text-slate-500">
            {canvasSize.width} &times; {canvasSize.height} canvas, shown at {Math.round(scale * 100)}%
          </p>
        </div>

        <div className="space-y-4">
          <Card>
            <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              {selected ? ELEMENT_LABELS[selected.type] : "Inspector"}
            </h3>
            {!selected ? (
              <p className="text-sm text-slate-500 dark:text-slate-400">Select an element to edit it.</p>
            ) : !canEdit ? (
              <dl className="space-y-1 text-sm text-slate-600 dark:text-slate-300">
                <div>
                  Position: {Math.round(selected.x)}, {Math.round(selected.y)}
                </div>
                <div>
                  Size: {Math.round(selected.width)} &times; {Math.round(selected.height)}
                </div>
              </dl>
            ) : (
              <div className="space-y-3">
                {TEXT_BINDING_TYPES.has(selected.type) && (
                  <Field label="Text" htmlFor="elText">
                    <Input
                      id="elText"
                      value={(selected.binding.text as string) || ""}
                      onChange={(e) => updateSelected({ binding: { ...selected.binding, text: e.target.value } })}
                    />
                  </Field>
                )}
                {selected.type === "logo" && (
                  <Field label="Image URL" htmlFor="elImageUrl">
                    <Input
                      id="elImageUrl"
                      placeholder="https://…/logo.png"
                      value={(selected.binding.image_url as string) || ""}
                      onChange={(e) =>
                        updateSelected({ binding: { ...selected.binding, image_url: e.target.value } })
                      }
                    />
                  </Field>
                )}
                {selected.type === "qr_code" && (
                  <Field label="QR value (URL or text)" htmlFor="elQrValue">
                    <Input
                      id="elQrValue"
                      value={(selected.binding.value as string) || ""}
                      onChange={(e) => updateSelected({ binding: { ...selected.binding, value: e.target.value } })}
                    />
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      A URL or plain text — rendered as a real scannable QR code, here and on the
                      public display.
                    </p>
                  </Field>
                )}
                <div className="grid grid-cols-2 gap-2">
                  <Field label="X" htmlFor="elX">
                    <Input
                      id="elX"
                      type="number"
                      value={Math.round(selected.x)}
                      onChange={(e) => updateSelected({ x: Number(e.target.value) })}
                    />
                  </Field>
                  <Field label="Y" htmlFor="elY">
                    <Input
                      id="elY"
                      type="number"
                      value={Math.round(selected.y)}
                      onChange={(e) => updateSelected({ y: Number(e.target.value) })}
                    />
                  </Field>
                  <Field label="Width" htmlFor="elW">
                    <Input
                      id="elW"
                      type="number"
                      value={Math.round(selected.width)}
                      onChange={(e) => updateSelected({ width: Number(e.target.value) })}
                    />
                  </Field>
                  <Field label="Height" htmlFor="elH">
                    <Input
                      id="elH"
                      type="number"
                      value={Math.round(selected.height)}
                      onChange={(e) => updateSelected({ height: Number(e.target.value) })}
                    />
                  </Field>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <Field label="Font size" htmlFor="elFont">
                    <Input
                      id="elFont"
                      type="number"
                      value={typeof selected.style.font_size === "number" ? selected.style.font_size : 32}
                      onChange={(e) =>
                        updateSelected({ style: { ...selected.style, font_size: Number(e.target.value) } })
                      }
                    />
                  </Field>
                  <Field label="Text color" htmlFor="elColor">
                    <input
                      id="elColor"
                      type="color"
                      className="h-9 w-full cursor-pointer rounded-md border border-slate-300 dark:border-slate-600"
                      value={(selected.style.color as string) || "#f5f7fa"}
                      onChange={(e) => updateSelected({ style: { ...selected.style, color: e.target.value } })}
                    />
                  </Field>
                </div>
                <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                  <input
                    type="checkbox"
                    className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
                    checked={selected.is_locked}
                    onChange={(e) => updateSelected({ is_locked: e.target.checked })}
                  />
                  Locked
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                  <input
                    type="checkbox"
                    className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
                    checked={selected.is_hidden}
                    onChange={(e) => updateSelected({ is_hidden: e.target.checked })}
                  />
                  Hidden
                </label>
                <Button
                  variant="danger"
                  onClick={() => {
                    setElements((prev) => prev.filter((e) => e.id !== selected.id));
                    setSelectedId(null);
                  }}
                >
                  Remove element
                </Button>
              </div>
            )}
          </Card>

          <Card>
            <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Layers</h3>
            {elements.length === 0 ? (
              <p className="text-sm text-slate-500 dark:text-slate-400">No elements yet.</p>
            ) : (
              <ul className="space-y-1 text-sm">
                {sortedForLayers.map((el) => (
                  <li key={el.id}>
                    <button
                      onClick={() => setSelectedId(el.id)}
                      className={`flex w-full items-center justify-between rounded px-2 py-1 text-left ${
                        selectedId === el.id
                          ? "bg-brand-50 text-brand-700 dark:bg-brand-900 dark:text-brand-300"
                          : "hover:bg-slate-50 text-slate-600 dark:text-slate-300 dark:hover:bg-slate-700"
                      }`}
                    >
                      <span>{ELEMENT_LABELS[el.type]}</span>
                      <span className="flex items-center gap-1 text-xs text-slate-400 dark:text-slate-500">
                        {el.is_locked && "🔒"}
                        {el.is_hidden && "🚫"}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
