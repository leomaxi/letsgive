import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCountdown } from "./useCountdown";

describe("useCountdown", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns null when there's no end time", () => {
    const { result } = renderHook(() => useCountdown(null));
    expect(result.current).toBeNull();
  });

  it("formats remaining time as m:ss", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    const endsAt = new Date("2026-01-01T00:05:30.000Z").toISOString();

    const { result } = renderHook(() => useCountdown(endsAt));
    expect(result.current).toBe("5:30");
  });

  it("pads seconds under 10", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    const endsAt = new Date("2026-01-01T00:01:05.000Z").toISOString();

    const { result } = renderHook(() => useCountdown(endsAt));
    expect(result.current).toBe("1:05");
  });

  it("ticks down as real time (fake-timer) advances", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    const endsAt = new Date("2026-01-01T00:00:10.000Z").toISOString();

    const { result } = renderHook(() => useCountdown(endsAt));
    expect(result.current).toBe("0:10");

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(result.current).toBe("0:07");
  });

  it("clamps to 0:00 once the end time has passed, never going negative", () => {
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    const endsAt = new Date("2026-01-01T00:00:05.000Z").toISOString();

    const { result } = renderHook(() => useCountdown(endsAt));

    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(result.current).toBe("0:00");
  });
});
