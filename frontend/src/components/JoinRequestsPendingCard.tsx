import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { joinRequestApi } from "@/lib/endpoints";
import { Badge, Button, Card, ErrorText, Spinner } from "@/components/ui";

export default function JoinRequestsPendingCard() {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["my-join-requests"],
    queryFn: joinRequestApi.listMine,
  });

  async function cancel(joinRequestId: string) {
    setError(null);
    setBusyId(joinRequestId);
    try {
      await joinRequestApi.cancelMine(joinRequestId);
      await queryClient.invalidateQueries({ queryKey: ["my-join-requests"] });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setBusyId(null);
    }
  }

  if (query.isLoading) {
    return (
      <Card>
        <Spinner className="h-5 w-5 text-brand-600" />
      </Card>
    );
  }

  if (!query.data || query.data.length === 0) return null;

  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Pending join requests
      </h2>
      <ErrorText>{error}</ErrorText>
      <ul className="divide-y divide-slate-100 dark:divide-slate-700">
        {query.data.map((req) => (
          <li key={req.id} className="flex items-center justify-between py-3 text-sm">
            <div>
              <div className="font-medium text-slate-700 dark:text-slate-300">{req.organization_name}</div>
              <div className="mt-1">
                <Badge tone="amber">Waiting for owner approval</Badge>
              </div>
            </div>
            <Button variant="secondary" disabled={busyId === req.id} onClick={() => cancel(req.id)}>
              Cancel request
            </Button>
          </li>
        ))}
      </ul>
    </Card>
  );
}
