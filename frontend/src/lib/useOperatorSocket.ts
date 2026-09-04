import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { sessionApi } from "@/lib/endpoints";
import type { SessionOperator } from "@/lib/types";

/**
 * Keeps the ["session", sessionId] query cache pushed live from
 * WS /v1/sessions/{id}/live-operator instead of relying on polling. A
 * background refetchInterval on that query stays on as a safety net in
 * case this socket silently stalls, so this hook only needs to handle the
 * common case well, not be bulletproof.
 */
export function useOperatorSocket(sessionId: string | undefined, enabled: boolean): boolean {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!sessionId || !enabled) {
      setConnected(false);
      return;
    }

    let cancelled = false;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelayMs = 1000;

    async function connect() {
      try {
        const res = await sessionApi.operatorSocketToken(sessionId!);
        if (cancelled) return;

        const proto = window.location.protocol === "https:" ? "wss://" : "ws://";
        const url = `${proto}${window.location.host}/v1/sessions/${sessionId}/live-operator?token=${encodeURIComponent(res.operator_socket_token)}`;
        ws = new WebSocket(url);

        ws.onopen = () => {
          retryDelayMs = 1000;
          setConnected(true);
        };
        ws.onmessage = (evt) => {
          try {
            const data = JSON.parse(evt.data) as SessionOperator;
            queryClient.setQueryData(["session", sessionId], data);
          } catch {
            /* ignore malformed frame */
          }
        };
        ws.onclose = () => {
          setConnected(false);
          if (cancelled) return;
          retryTimer = setTimeout(connect, retryDelayMs);
          retryDelayMs = Math.min(retryDelayMs * 1.7, 15000);
        };
        ws.onerror = () => ws?.close();
      } catch {
        if (!cancelled) retryTimer = setTimeout(connect, retryDelayMs);
      }
    }

    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      ws?.close();
    };
  }, [sessionId, enabled, queryClient]);

  return connected;
}
