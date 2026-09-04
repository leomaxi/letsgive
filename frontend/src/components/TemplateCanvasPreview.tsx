import type { DisplayElementInput } from "@/lib/endpoints";
import { elementPreviewStylePercent, previewText } from "@/lib/templateElementStyle";

// A read-only, non-interactive render of a template's canvas -- the same
// layout math and placeholder text the Display Studio editor uses, scaled
// with plain CSS percentages so it stays responsive inside a grid card
// instead of needing a fixed pixel width. Used anywhere someone needs to
// see what a template actually looks like without opening the full editor
// (the preset picker, the template list's cards).
export default function TemplateCanvasPreview({
  canvas,
  elements,
  className = "",
}: {
  canvas: { width: number; height: number; background_color: string };
  elements: DisplayElementInput[];
  className?: string;
}) {
  return (
    <div
      className={`relative w-full overflow-hidden rounded-md ${className}`}
      style={{ backgroundColor: canvas.background_color, aspectRatio: `${canvas.width} / ${canvas.height}` }}
    >
      {[...elements]
        .sort((a, b) => a.z_index - b.z_index)
        .map((el, i) => (
          <div
            key={i}
            className="select-none truncate"
            style={elementPreviewStylePercent(el, canvas.width, canvas.height)}
          >
            <span className="truncate">{previewText(el)}</span>
          </div>
        ))}
    </div>
  );
}
