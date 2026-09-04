import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { orgApi } from "@/lib/endpoints";
import type { Role } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Input, Label, Spinner } from "@/components/ui";

const ROLES: Role[] = ["owner", "finance", "media", "auditor"];

const ROLE_TONE: Record<Role, "blue" | "green" | "amber" | "slate"> = {
  owner: "blue",
  finance: "green",
  media: "amber",
  auditor: "slate",
  system_admin: "slate",
};

export default function OrgHomePage() {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("media");
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviteSuccess, setInviteSuccess] = useState<string | null>(null);

  const membersQuery = useQuery({
    queryKey: ["members", activeOrg?.id],
    queryFn: () => orgApi.listMembers(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const inviteMutation = useMutation({
    mutationFn: () => orgApi.inviteMember(activeOrg!.id, email, role),
    onSuccess: () => {
      setInviteSuccess(`Invited ${email} as ${role}.`);
      setEmail("");
      queryClient.invalidateQueries({ queryKey: ["members", activeOrg?.id] });
    },
    onError: (err) => {
      setInviteError(err instanceof ApiError ? err.message : "Could not send the invitation.");
    },
  });

  if (!activeOrg) return null;

  function onInvite(e: FormEvent) {
    e.preventDefault();
    setInviteError(null);
    setInviteSuccess(null);
    inviteMutation.mutate();
  }

  const isOwner = activeOrg.role === "owner";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">{activeOrg.name}</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          {activeOrg.country} · {activeOrg.currency} · {activeOrg.timezone}
        </p>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Subscription
          </h2>
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between">
              <dt className="text-slate-500 dark:text-slate-400">Status</dt>
              <dd>
                <Badge tone={activeOrg.subscription_status === "canceled" ? "red" : "green"}>
                  {activeOrg.subscription_status}
                </Badge>
              </dd>
            </div>
            {activeOrg.grace_period_ends_at && (
              <div className="flex justify-between">
                <dt className="text-slate-500 dark:text-slate-400">Reports readable until</dt>
                <dd className="text-slate-700 dark:text-slate-300">
                  {new Date(activeOrg.grace_period_ends_at).toLocaleDateString()}
                </dd>
              </div>
            )}
          </dl>
        </Card>

        <Card>
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Members
          </h2>
          {membersQuery.isLoading ? (
            <Spinner className="h-5 w-5 text-brand-600" />
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-700">
              {membersQuery.data?.map((m) => (
                <li key={m.id} className="flex items-center justify-between py-2 text-sm">
                  <div>
                    <div className="text-slate-700 dark:text-slate-300">{m.user_full_name}</div>
                    <div className="text-xs text-slate-400 dark:text-slate-500">{m.user_email}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge tone={ROLE_TONE[m.role]}>{m.role}</Badge>
                    {m.status !== "active" && <Badge tone="amber">{m.status}</Badge>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {isOwner && (
        <Card className="max-w-md">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Invite a teammate
          </h2>
          <form onSubmit={onInvite} className="space-y-4" noValidate>
            <Field label="Email" htmlFor="inviteEmail">
              <Input
                id="inviteEmail"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
            <div>
              <Label htmlFor="inviteRole">Role</Label>
              <select
                id="inviteRole"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
                value={role}
                onChange={(e) => setRole(e.target.value as Role)}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              {(role === "owner" || role === "finance") && (
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  This role requires the invitee to already have MFA enabled on their account.
                </p>
              )}
            </div>
            <ErrorText>{inviteError}</ErrorText>
            {inviteSuccess && (
              <p className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                {inviteSuccess}
              </p>
            )}
            <Button type="submit" disabled={inviteMutation.isPending}>
              {inviteMutation.isPending ? "Sending…" : "Send invitation"}
            </Button>
          </form>
        </Card>
      )}
    </div>
  );
}
