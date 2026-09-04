import { describe, expect, it } from "vitest";
import { elementPreviewStylePercent, previewText } from "@/lib/templateElementStyle";
import type { DisplayElementInput } from "@/lib/endpoints";

function makeEl(overrides: Partial<DisplayElementInput> = {}): DisplayElementInput {
  return {
    type: "heading",
    x: 100,
    y: 200,
    width: 400,
    height: 100,
    z_index: 1,
    style: {},
    binding: {},
    is_locked: false,
    is_hidden: false,
    ...overrides,
  };
}

describe("previewText", () => {
  it("uses the bound text for text elements, falling back to the type's label when empty", () => {
    expect(previewText(makeEl({ type: "heading", binding: { text: "Welcome" } }))).toBe("Welcome");
    expect(previewText(makeEl({ type: "heading", binding: {} }))).toBe("Heading");
    expect(previewText(makeEl({ type: "sponsor_message", binding: {} }))).toBe("Sponsor message");
  });

  it("shows placeholder sample values for data-bound elements, not their real (session-less) data", () => {
    expect(previewText(makeEl({ type: "contribution_count" }))).toBe("128");
    expect(previewText(makeEl({ type: "amount" }))).toBe("$1,234");
    expect(previewText(makeEl({ type: "goal" }))).toBe("$5,000 goal");
    expect(previewText(makeEl({ type: "countdown" }))).toBe("9:45");
  });

  it("shows a fallback label for logo/QR only when no real binding value is set", () => {
    expect(previewText(makeEl({ type: "logo", binding: {} }))).toBe("LOGO");
    expect(previewText(makeEl({ type: "logo", binding: { image_url: "https://x/logo.png" } }))).toBe("");
    expect(previewText(makeEl({ type: "qr_code", binding: {} }))).toBe("[ QR ]");
    expect(previewText(makeEl({ type: "qr_code", binding: { value: "https://give.example.org" } }))).toBe("");
  });

  it("renders progress_bar and background with no text overlay", () => {
    expect(previewText(makeEl({ type: "progress_bar" }))).toBe("");
    expect(previewText(makeEl({ type: "background" }))).toBe("");
  });
});

describe("elementPreviewStylePercent", () => {
  it("converts absolute canvas coordinates to percentages of the canvas size", () => {
    const style = elementPreviewStylePercent(makeEl({ x: 480, y: 540, width: 960, height: 270 }), 1920, 1080);
    expect(style.left).toBe("25%");
    expect(style.top).toBe("50%");
    expect(style.width).toBe("50%");
    expect(style.height).toBe("25%");
  });

  it("maps text_align to a matching flex justification, defaulting to center", () => {
    expect(elementPreviewStylePercent(makeEl({ style: { text_align: "left" } }), 1920, 1080).justifyContent).toBe(
      "flex-start"
    );
    expect(elementPreviewStylePercent(makeEl({ style: { text_align: "right" } }), 1920, 1080).justifyContent).toBe(
      "flex-end"
    );
    expect(elementPreviewStylePercent(makeEl({ style: {} }), 1920, 1080).justifyContent).toBe("center");
  });

  it("falls back to a translucent background only for the background element type, otherwise transparent", () => {
    expect(elementPreviewStylePercent(makeEl({ type: "background", style: {} }), 1920, 1080).backgroundColor).toBe(
      "rgba(255,255,255,0.06)"
    );
    expect(elementPreviewStylePercent(makeEl({ type: "heading", style: {} }), 1920, 1080).backgroundColor).toBe(
      "transparent"
    );
    expect(
      elementPreviewStylePercent(makeEl({ type: "heading", style: { background_color: "#ff0000" } }), 1920, 1080)
        .backgroundColor
    ).toBe("#ff0000");
  });

  it("dims hidden elements instead of hiding them outright, so a preset preview still shows its full layout", () => {
    expect(elementPreviewStylePercent(makeEl({ is_hidden: true }), 1920, 1080).opacity).toBe(0.3);
    expect(elementPreviewStylePercent(makeEl({ is_hidden: false }), 1920, 1080).opacity).toBe(1);
  });
});
