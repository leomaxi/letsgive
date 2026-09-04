import type { CSSProperties } from "react";
import type { DisplayElementInput } from "@/lib/endpoints";
import type { ElementType } from "@/lib/types";

export const ELEMENT_LABELS: Record<ElementType, string> = {
  logo: "Logo",
  heading: "Heading",
  body_text: "Body text",
  contribution_count: "Contribution count",
  amount: "Amount",
  goal: "Goal",
  progress_bar: "Progress bar",
  countdown: "Countdown",
  payment_instructions: "Payment instructions",
  qr_code: "QR code",
  background: "Background",
  sponsor_message: "Sponsor message",
};

// Placeholder copy shown in the editor canvas and in preset previews --
// neither has a live session to pull a real count/amount/countdown from.
export function previewText(el: Pick<DisplayElementInput, "type" | "binding">): string {
  if (el.type === "heading" || el.type === "body_text" || el.type === "sponsor_message" || el.type === "payment_instructions") {
    return (el.binding.text as string) || ELEMENT_LABELS[el.type];
  }
  switch (el.type) {
    case "contribution_count":
      return "128";
    case "amount":
      return "$1,234";
    case "goal":
      return "$5,000 goal";
    case "countdown":
      return "9:45";
    case "qr_code":
      return el.binding.value ? "" : "[ QR ]";
    case "logo":
      return el.binding.image_url ? "" : "LOGO";
    case "progress_bar":
      return "";
    case "background":
      return "";
    default:
      return ELEMENT_LABELS[el.type];
  }
}

// The same per-element layout math the editor's canvas and the preset
// picker's preview both need: position/size scaled to the render width,
// text alignment turned into a flex justification, and the same style
// fallbacks (default text color, background, dashed outline) so a preset
// preview looks like what you'd actually get, not an approximation.
export function elementPreviewStyle(el: DisplayElementInput, scale: number): CSSProperties {
  const align = (el.style.text_align as string) || "center";
  const justify = align === "left" ? "flex-start" : align === "right" ? "flex-end" : "center";
  const fontSize = (typeof el.style.font_size === "number" ? el.style.font_size : 32) * scale;

  return {
    position: "absolute",
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
    overflow: "hidden",
  };
}

// A percentage-based variant for previews that need to stay responsive to
// their container's width (a card in a grid) rather than a fixed pixel
// canvas -- CSS alone handles the scaling, no ResizeObserver needed. Font
// size intentionally stays a small constant rather than scaling with the
// canvas: at preview sizes what matters is the layout/composition (where
// things sit, what colors), not proportionally exact type -- and a constant
// keeps text legible instead of shrinking to a few illegible pixels.
export function elementPreviewStylePercent(
  el: DisplayElementInput,
  canvasWidth: number,
  canvasHeight: number
): CSSProperties {
  const align = (el.style.text_align as string) || "center";
  const justify = align === "left" ? "flex-start" : align === "right" ? "flex-end" : "center";

  return {
    position: "absolute",
    left: `${(el.x / canvasWidth) * 100}%`,
    top: `${(el.y / canvasHeight) * 100}%`,
    width: `${(el.width / canvasWidth) * 100}%`,
    height: `${(el.height / canvasHeight) * 100}%`,
    display: "flex",
    alignItems: "center",
    justifyContent: justify,
    textAlign: align as "left" | "right" | "center",
    color: (el.style.color as string) || "#f5f7fa",
    backgroundColor:
      (el.style.background_color as string) ||
      (el.type === "background" ? "rgba(255,255,255,0.06)" : "transparent"),
    fontSize: 10,
    lineHeight: 1.2,
    opacity: el.is_hidden ? 0.3 : typeof el.style.opacity === "number" ? el.style.opacity : 1,
    padding: 2,
    overflow: "hidden",
  };
}
