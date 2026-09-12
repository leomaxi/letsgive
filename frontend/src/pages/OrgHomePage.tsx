import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { orgApi, sessionApi } from "@/lib/endpoints";
import type { Role } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Input, Label, PageHeader, Select, Spinner, StatCard } from "@/components/ui";
import JoinRequestsCard from "@/components/JoinRequestsCard";

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
  const [roleError, setRoleError] = useState<string | null>(null);
  const [roleSuccess, setRoleSuccess] = useState<string | null>(null);
  const [codeCopied, setCodeCopied] = useState(false);

  const membersQuery = useQuery({
    queryKey: ["members", activeOrg?.id],
    queryFn: () => orgApi.listMembers(activeOrg!.id),
    enabled: !!activeOrg,
  });
  const sessionsQuery = useQuery({
    queryKey: ["sessions", activeOrg?.id],
    queryFn: () => sessionApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
    refetchInterval: 5000,
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

  const roleMutation = useMutation({
    mutationFn: ({ memberId, nextRole }: { memberId: string; nextRole: Role }) =>
      orgApi.updateMemberRole(activeOrg!.id, memberId, nextRole),
    onSuccess: (member) => {
      setRoleError(null);
      setRoleSuccess(`Updated ${member.user_email} to ${member.role}.`);
      queryClient.invalidateQueries({ queryKey: ["members", activeOrg?.id] });
      queryClient.invalidateQueries({ queryKey: ["my-organizations"] });
    },
    onError: (err) => {
      setRoleSuccess(null);
      setRoleError(err instanceof ApiError ? err.message : "Could not update this member's role.");
    },
  });

  if (!activeOrg) return null;

  function onInvite(e: FormEvent) {
    e.preventDefault();
    setInviteError(null);
    setInviteSuccess(null);
    inviteMutation.mutate();
  }

  function onRoleChange(memberId: string, nextRole: Role) {
    setRoleError(null);
    setRoleSuccess(null);
    roleMutation.mutate({ memberId, nextRole });
  }

  const isOwner = activeOrg.role === "owner";
  const canManageBilling = activeOrg.role === "owner" || activeOrg.role === "finance";
  const canCreateSession =
    activeOrg.role === "owner" || activeOrg.role === "media" || activeOrg.role === "finance";
  const sessions = sessionsQuery.data ?? [];
  const liveSessions = sessions.filter((session) => session.status === "live");
  const setupSessions = sessions.filter((session) =>
    ["draft", "approval_requested", "authorized"].includes(session.status)
  );
  const connectedSessions = sessions.filter((session) => !!session.mailbox_connection_id).length;

  return (
    <div className="space-y-6">
      <PageHeader
        title={activeOrg.name}
        description={`${activeOrg.country} · ${activeOrg.currency} · ${activeOrg.timezone}`}
        actions={
          canCreateSession ? (
            <Link to="/sessions/new">
              <Button>New session</Button>
            </Link>
          ) : null
        }
      />

      <div className="grid gap-4 md:grid-cols-3">
        <StatCard
          label="Live sessions"
          value={liveSessions.length}
          detail={liveSessions.length > 0 ? "Open the session page before presenting." : "No active monitoring right now."}
          tone={liveSessions.length > 0 ? "green" : "slate"}
        />
        <StatCard
          label="In setup"
          value={setupSessions.length}
          detail="Drafts, approvals, and ready-to-start sessions."
          tone={setupSessions.length > 0 ? "amber" : "slate"}
        />
        <StatCard
          label="Mailbox linked"
          value={`${connectedSessions}/${sessions.length || 0}`}
          detail="Sessions with a connected deposit inbox."
          tone={connectedSessions > 0 ? "blue" : "slate"}
        />
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
          <div className="mt-4 flex flex-wrap gap-2">
            {canManageBilling && (
              <Link to="/billing">
                <Button variant="secondary">Manage billing</Button>
              </Link>
            )}
            <Link to="/support">
              <Button variant="secondary">Contact support</Button>
            </Link>
          </div>
        </Card>

        <Card>
          <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Organization code
          </h2>
          <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
            Share this with a teammate -- they can enter it when they sign up to request access, and
            you approve them with a role.
          </p>
          <div className="flex items-center gap-2">
            <code className="rounded-md bg-slate-100 px-3 py-2 text-lg font-semibold tracking-widest text-slate-800 dark:bg-slate-900 dark:text-slate-100">
              {activeOrg.join_code}
            </code>
            <Button
              variant="secondary"
              onClick={() => {
                navigator.clipboard.writeText(activeOrg.join_code).then(() => {
                  setCodeCopied(true);
                  setTimeout(() => setCodeCopied(false), 2000);
                });
              }}
            >
              {codeCopied ? "Copied!" : "Copy"}
            </Button>
          </div>
        </Card>

        <Card>
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Members
          </h2>
          {membersQuery.isLoading ? (
            <Spinner className="h-5 w-5 text-brand-600" />
          ) : (
            <>
              <ul className="divide-y divide-slate-100 dark:divide-slate-700">
                {membersQuery.data?.map((m) => (
                  <li key={m.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                    <div className="min-w-0">
                      <div className="truncate text-slate-700 dark:text-slate-300">{m.user_full_name}</div>
                      <div className="truncate text-xs text-slate-400 dark:text-slate-500">{m.user_email}</div>
                    </div>
                    <div className="flex flex-shrink-0 items-center gap-2">
                      {isOwner ? (
                        <Select
                          className="w-28 py-1"
                          value={m.role}
                          disabled={roleMutation.isPending}
                          onChange={(e) => onRoleChange(m.id, e.target.value as Role)}
                        >
                          {ROLES.map((r) => (
                            <option key={r} value={r}>
                              {r}
                            </option>
                          ))}
                        </Select>
                      ) : (
                        <Badge tone={ROLE_TONE[m.role]}>{m.role}</Badge>
                      )}
                      {m.status !== "active" && <Badge tone="amber">{m.status}</Badge>}
                    </div>
                  </li>
                ))}
              </ul>
              <ErrorText>{roleError}</ErrorText>
              {roleSuccess && (
                <p className="mt-3 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                  {roleSuccess}
                </p>
              )}
            </>
          )}
        </Card>
      </div>

      {isOwner && <JoinRequestsCard />}

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
              <Select
                id="inviteRole"
                value={role}
                onChange={(e) => setRole(e.target.value as Role)}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </Select>
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
