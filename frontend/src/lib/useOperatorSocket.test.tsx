import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useOperatorSocket } from "./useOperatorSocket";
import { sessionApi } from "@/lib/endpoints";
import type { SessionOperator } from "@/lib/types";

vi.mock("@/lib/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/endpoints")>();
  return {
    ...actual,
    sessionApi: { ...actual.sessionApi, operatorSocketToken: vi.fn() },
  };
});

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((evt: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.();
  }
}

function makeOperator(overrides: Partial<SessionOperator> = {}): SessionOperator {
  return {
    id: "s1",
    organization_id: "org1",
    mailbox_connection_id: null,
    display_template_id: null,
    status: "live",
    version: 2,
    contribution_method: "e-transfer",
    currency: "CAD",
    duration_seconds: 600,
    starts_at: null,
    ends_at: null,
    watermark: null,
    goal_enabled: false,
    goal_amount: null,
    amount_visible: false,
    test_mode: false,
    operator_warning: null,
    contribution_count: 3,
    total_amount: null,
    ...overrides,
  };
}

describe("useOperatorSocket", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
    vi.mocked(sessionApi.operatorSocketToken).mockResolvedValue({
      operator_socket_token: "tok-123",
      expires_in_minutes: 60,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function renderWithClient(sessionId: string | undefined, enabled: boolean) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function Wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
    }
    const view = renderHook(({ id, en }) => useOperatorSocket(id, en), {
      wrapper: Wrapper,
      initialProps: { id: sessionId, en: enabled },
    });
    return { ...view, queryClient };
  }

  it("does nothing when disabled", async () => {
    renderWithClient("s1", false);
    await act(async () => {
      await Promise.resolve();
    });
    expect(sessionApi.operatorSocketToken).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("connects with a token minted for the given session and reports connected on open", async () => {
    const { result } = renderWithClient("s1", true);

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(sessionApi.operatorSocketToken).toHaveBeenCalledWith("s1");

    const socket = FakeWebSocket.instances[0];
    expect(socket.url).toContain("/v1/sessions/s1/live-operator");
    expect(socket.url).toContain("token=tok-123");
    expect(result.current).toBe(false);

    act(() => socket.onopen?.());
    expect(result.current).toBe(true);
  });

  it("pushes incoming messages straight into the ['session', id] query cache", async () => {
    const { queryClient } = renderWithClient("s1", true);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onopen?.());

    const payload = makeOperator({ contribution_count: 7 });
    act(() => socket.onmessage?.({ data: JSON.stringify(payload) }));

    expect(queryClient.getQueryData(["session", "s1"])).toEqual(payload);
  });

  it("ignores a malformed frame instead of throwing", async () => {
    const { queryClient } = renderWithClient("s1", true);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onopen?.());

    expect(() => {
      act(() => socket.onmessage?.({ data: "{not json" }));
    }).not.toThrow();
    expect(queryClient.getQueryData(["session", "s1"])).toBeUndefined();
  });

  it("reports disconnected when the socket closes", async () => {
    const { result } = renderWithClient("s1", true);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onopen?.());
    expect(result.current).toBe(true);

    act(() => socket.close());
    expect(result.current).toBe(false);
  });

  it("closes the socket on unmount and doesn't reconnect afterward", async () => {
    const { unmount } = renderWithClient("s1", true);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onopen?.());

    unmount();
    expect(socket.closed).toBe(true);

    const countAfterUnmount = FakeWebSocket.instances.length;
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1100));
    });
    expect(FakeWebSocket.instances).toHaveLength(countAfterUnmount);
  }, 3000);
});
