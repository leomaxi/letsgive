import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { joinRequestApi } from "@/lib/endpoints";
import type { Role } from "@/lib/types";
import { Button, Card, ErrorText, Spinner } from "@/components/ui";

const ROLES: Role[] = ["owner", "finance", "media", "auditor"];

function JoinRequestRow({ requestId, userEmail, userFullName }: { requestId: string; userEmail: string; userFullName: string }) {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();
  const [role, setRole] = useState<Role>("media");
  const [error, setError] = useState<string | null>(null);

  const approveMutation = useMutation({
    mutationFn: () => joinRequestApi.approve(activeOrg!.id, requestId, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["join-requests", activeOrg?.id] });
      queryClient.invalidateQueries({ queryKey: ["members", activeOrg?.id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not approve this request."),
  });

  const denyMutation = useMutation({
    mutationFn: () => joinRequestApi.deny(activeOrg!.id, requestId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["join-requests", activeOrg?.id] }),
  });

  const busy = approveMutation.isPending || denyMutation.isPending;

  return (
    <li className="py-3 text-sm">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-medium text-slate-700 dark:text-slate-300">{userFullName}</div>
          <div className="text-xs text-slate-400 dark:text-slate-500">{userEmail}</div>
        </div>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <select
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
          value={role}
          onChange={(e) => setRole(e.target.value as Role)}
          disabled={busy}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <Button disabled={busy} onClick={() => approveMutation.mutate()}>
          {approveMutation.isPending ? "Approving…" : "Approve"}
        </Button>
        <Button variant="secondary" disabled={busy} onClick={() => denyMutation.mutate()}>
          Deny
        </Button>
      </div>
      {(role === "owner" || role === "finance") && (
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          This role requires the requester to already have MFA enabled.
        </p>
      )}
      <ErrorText>{error}</ErrorText>
    </li>
  );
}

export default function JoinRequestsCard() {
  const { activeOrg } = useOrg();

  const query = useQuery({
    queryKey: ["join-requests", activeOrg?.id],
    queryFn: () => joinRequestApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

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
      <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Join requests
      </h2>
      <p className="mb-2 text-sm text-slate-500 dark:text-slate-400">
        Someone entered your organization code and is waiting for a role.
      </p>
      <ul className="divide-y divide-slate-100 dark:divide-slate-700">
        {query.data.map((req) => (
          <JoinRequestRow
            key={req.id}
            requestId={req.id}
            userEmail={req.user_email}
            userFullName={req.user_full_name}
          />
        ))}
      </ul>
    </Card>
  );
}
