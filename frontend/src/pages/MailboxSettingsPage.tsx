import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { connectionApi, parserProfileApi } from "@/lib/endpoints";
import type { ConnectionStatus, ParserProfile } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Input, Spinner } from "@/components/ui";

const CONNECTION_STATUS_TONE: Record<ConnectionStatus, "slate" | "green" | "red" | "amber"> = {
  pending: "amber",
  connected: "green",
  error: "red",
  revoked: "slate",
};

function CheckNowButton({ connectionId, canViewLog }: { connectionId: string; canViewLog: boolean }) {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();
  const [result, setResult] = useState<string | null>(null);

  const checkMutation = useMutation({
    mutationFn: () => connectionApi.checkNow(activeOrg!.id, connectionId),
    onSuccess: (res) => {
      setResult(
        res.error
          ? `Error: ${res.error}`
          : `Checked: ${res.fetched} new message${res.fetched === 1 ? "" : "s"}, ${res.accepted} counted`,
      );
      queryClient.invalidateQueries({ queryKey: ["connections", activeOrg?.id] });
      queryClient.invalidateQueries({ queryKey: ["recent-imap-messages", activeOrg?.id, connectionId] });
    },
    onError: (err) => setResult(err instanceof ApiError ? err.message : "Could not check the mailbox."),
  });

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex gap-2">
        <Button variant="secondary" disabled={checkMutation.isPending} onClick={() => checkMutation.mutate()}>
          {checkMutation.isPending ? "Checking…" : "Check now"}
        </Button>
        {canViewLog && (
          <Link to={`/mailbox/${connectionId}/log`}>
            <Button variant="secondary">View log</Button>
          </Link>
        )}
      </div>
      {result && <span className="text-xs text-slate-500 dark:text-slate-400">{result}</span>}
    </div>
  );
}

function ConnectionsSection() {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();

  const canManage = activeOrg?.role === "owner" || activeOrg?.role === "finance";
  const canCheckNow = canManage || activeOrg?.role === "media";

  const [providerKind, setProviderKind] = useState<"imap" | "fake">("imap");
  const [mailbox, setMailbox] = useState("");
  const [imapPassword, setImapPassword] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [imapHost, setImapHost] = useState("");
  const [imapPort, setImapPort] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const connectionsQuery = useQuery({
    queryKey: ["connections", activeOrg?.id],
    queryFn: () => connectionApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      connectionApi.create(activeOrg!.id, {
        provider: providerKind,
        mailbox,
        ...(providerKind === "imap"
          ? {
              imap_password: imapPassword,
              imap_host: imapHost || undefined,
              imap_port: imapPort ? Number(imapPort) : undefined,
            }
          : {}),
      }),
    onSuccess: () => {
      setMailbox("");
      setImapPassword("");
      setImapHost("");
      setImapPort("");
      setShowAdvanced(false);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["connections", activeOrg?.id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not connect that mailbox."),
  });

  const revokeMutation = useMutation({
    mutationFn: (connectionId: string) => connectionApi.revoke(activeOrg!.id, connectionId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["connections", activeOrg?.id] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (connectionId: string) => connectionApi.remove(activeOrg!.id, connectionId),
    onSuccess: () => {
      setConfirmingDeleteId(null);
      setDeleteError(null);
      queryClient.invalidateQueries({ queryKey: ["connections", activeOrg?.id] });
    },
    onError: (err) =>
      setDeleteError(err instanceof ApiError ? err.message : "Could not delete that connection."),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    createMutation.mutate();
  }

  return (
    <Card>
      <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Mailbox connections
      </h2>
      <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
        A session needs a connected mailbox to receive live deposit notifications; without one it
        can still run in test mode.
      </p>

      {connectionsQuery.isLoading ? (
        <Spinner className="h-5 w-5 text-brand-600" />
      ) : connectionsQuery.data && connectionsQuery.data.length > 0 ? (
        <ul className="mb-4 divide-y divide-slate-100 text-sm dark:divide-slate-700">
          {connectionsQuery.data.map((c) => (
            <li key={c.id} className="flex items-center justify-between py-2">
              <div>
                <div className="text-slate-700 dark:text-slate-300">{c.mailbox}</div>
                <div className="text-xs text-slate-400 dark:text-slate-500">
                  {c.provider}
                  {c.last_sync_at && ` · last checked ${new Date(c.last_sync_at).toLocaleString()}`}
                  {c.webhook_health && c.webhook_health !== "ok" && (
                    <span className="text-amber-600 dark:text-amber-400"> · {c.webhook_health}</span>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Badge tone={CONNECTION_STATUS_TONE[c.status]}>{c.status}</Badge>
                {canCheckNow && c.status === "connected" && c.provider === "imap" && (
                  <CheckNowButton connectionId={c.id} canViewLog={canManage} />
                )}
                {canManage && c.status !== "revoked" && (
                  <Button
                    variant="secondary"
                    disabled={revokeMutation.isPending}
                    onClick={() => revokeMutation.mutate(c.id)}
                  >
                    Revoke
                  </Button>
                )}
                {canManage && c.status === "revoked" && (
                  confirmingDeleteId === c.id ? (
                    <>
                      <Button
                        variant="danger"
                        disabled={deleteMutation.isPending}
                        onClick={() => deleteMutation.mutate(c.id)}
                      >
                        Confirm delete
                      </Button>
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setConfirmingDeleteId(null);
                          setDeleteError(null);
                        }}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <Button
                      variant="secondary"
                      onClick={() => {
                        setConfirmingDeleteId(c.id);
                        setDeleteError(null);
                      }}
                    >
                      Delete
                    </Button>
                  )
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">No mailbox connections yet.</p>
      )}
      <ErrorText>{deleteError}</ErrorText>

      {!canManage && (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Only Owners and Finance officers can connect or revoke a mailbox.
        </p>
      )}

      {canManage && (
      <form onSubmit={onSubmit} className="space-y-3" noValidate>
        <div className="flex gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="providerKind"
              checked={providerKind === "imap"}
              onChange={() => setProviderKind("imap")}
              className="text-brand-600 focus:ring-brand-500"
            />
            Connect your email
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="providerKind"
              checked={providerKind === "fake"}
              onChange={() => setProviderKind("fake")}
              className="text-brand-600 focus:ring-brand-500"
            />
            Test mailbox (simulated, for rehearsal)
          </label>
        </div>

        <Field label="Email address" htmlFor="mailbox">
          <Input
            id="mailbox"
            type="email"
            placeholder="deposits@yourchurch.org"
            required
            value={mailbox}
            onChange={(e) => setMailbox(e.target.value)}
          />
        </Field>

        {providerKind === "imap" && (
          <>
            <Field label="App password" htmlFor="imapPassword">
              <Input
                id="imapPassword"
                type="password"
                autoComplete="new-password"
                required
                value={imapPassword}
                onChange={(e) => setImapPassword(e.target.value)}
              />
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                Works automatically with Gmail, Outlook/Office 365, Yahoo, and iCloud. With
                two-factor authentication turned on (or to allow IMAP access for an external app
                at all), these providers require an <strong>app-specific password</strong> instead
                of your regular one — look for "app passwords" in your email provider's account
                security settings and generate one to paste here. Your regular password is never
                stored.
              </p>
            </Field>

            {!showAdvanced ? (
              <button
                type="button"
                onClick={() => setShowAdvanced(true)}
                className="text-xs text-brand-600 hover:text-brand-700"
              >
                Using a different email provider? Set a custom mail server
              </button>
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <Field label="IMAP server" htmlFor="imapHost">
                  <Input
                    id="imapHost"
                    placeholder="imap.example.com"
                    value={imapHost}
                    onChange={(e) => setImapHost(e.target.value)}
                  />
                </Field>
                <Field label="Port" htmlFor="imapPort">
                  <Input
                    id="imapPort"
                    type="number"
                    placeholder="993"
                    value={imapPort}
                    onChange={(e) => setImapPort(e.target.value)}
                  />
                </Field>
              </div>
            )}
          </>
        )}

        <Button type="submit" disabled={createMutation.isPending}>
          {createMutation.isPending
            ? providerKind === "imap"
              ? "Connecting…"
              : "Adding…"
            : providerKind === "imap"
              ? "Connect mailbox"
              : "Add"}
        </Button>
      </form>
      )}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function ParserProfileEditForm({
  profile,
  onDone,
}: {
  profile: ParserProfile;
  onDone: () => void;
}) {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();

  const [name, setName] = useState(profile.name);
  const [senderPatterns, setSenderPatterns] = useState(profile.sender_patterns.join(", "));
  const [confidenceThreshold, setConfidenceThreshold] = useState(profile.confidence_threshold);
  const [isActive, setIsActive] = useState(profile.is_active);
  const [error, setError] = useState<string | null>(null);

  const updateMutation = useMutation({
    mutationFn: () =>
      parserProfileApi.update(activeOrg!.id, profile.id, {
        name,
        sender_patterns: senderPatterns
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        confidence_threshold: confidenceThreshold,
        is_active: isActive,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["parser-profiles", activeOrg?.id] });
      onDone();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not save changes."),
  });

  return (
    <li className="space-y-3 py-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Profile name" htmlFor={`editName-${profile.id}`}>
          <Input id={`editName-${profile.id}`} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Confidence threshold (0–1)" htmlFor={`editConfidence-${profile.id}`}>
          <Input
            id={`editConfidence-${profile.id}`}
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={confidenceThreshold}
            onChange={(e) => setConfidenceThreshold(Number(e.target.value))}
          />
        </Field>
      </div>
      <Field label="Sender addresses/domains (comma-separated)" htmlFor={`editPatterns-${profile.id}`}>
        <Input
          id={`editPatterns-${profile.id}`}
          value={senderPatterns}
          onChange={(e) => setSenderPatterns(e.target.value)}
        />
      </Field>
      <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
        <input
          type="checkbox"
          className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
          checked={isActive}
          onChange={(e) => setIsActive(e.target.checked)}
        />
        Active
      </label>
      <ErrorText>{error}</ErrorText>
      <div className="flex gap-2">
        <Button disabled={updateMutation.isPending} onClick={() => updateMutation.mutate()}>
          {updateMutation.isPending ? "Saving…" : "Save"}
        </Button>
        <Button variant="secondary" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </li>
  );
}

function ParserProfilesSection() {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();

  const canManage = activeOrg?.role === "owner" || activeOrg?.role === "finance";

  const [name, setName] = useState("");
  const [senderPatterns, setSenderPatterns] = useState("");
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.75);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);

  const profilesQuery = useQuery({
    queryKey: ["parser-profiles", activeOrg?.id],
    queryFn: () => parserProfileApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      parserProfileApi.create(activeOrg!.id, {
        name,
        sender_patterns: senderPatterns
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        confidence_threshold: confidenceThreshold,
      }),
    onSuccess: () => {
      setName("");
      setSenderPatterns("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["parser-profiles", activeOrg?.id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not create the profile."),
  });

  const deleteMutation = useMutation({
    mutationFn: (profileId: string) => parserProfileApi.remove(activeOrg!.id, profileId),
    onSuccess: () => {
      setConfirmingDeleteId(null);
      queryClient.invalidateQueries({ queryKey: ["parser-profiles", activeOrg?.id] });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    createMutation.mutate();
  }

  return (
    <Card>
      <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Parser profiles
      </h2>
      <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
        Which sender addresses count as your bank/payment provider, and how confident a match needs
        to be before a deposit counts automatically.
      </p>

      {profilesQuery.isLoading ? (
        <Spinner className="h-5 w-5 text-brand-600" />
      ) : profilesQuery.data && profilesQuery.data.length > 0 ? (
        <ul className="mb-4 divide-y divide-slate-100 text-sm dark:divide-slate-700">
          {profilesQuery.data.map((p) =>
            editingId === p.id ? (
              <ParserProfileEditForm key={p.id} profile={p} onDone={() => setEditingId(null)} />
            ) : (
              <li key={p.id} className="py-2">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-700 dark:text-slate-300">{p.name}</span>
                  <Badge tone={p.is_active ? "green" : "slate"}>
                    {p.is_active ? "active" : "inactive"}
                  </Badge>
                </div>
                <div className="text-xs text-slate-400 dark:text-slate-500">
                  {p.sender_patterns.join(", ")} · confidence ≥ {p.confidence_threshold}
                </div>
                {canManage && (
                  <div className="mt-2 flex gap-2">
                    <Button variant="secondary" onClick={() => setEditingId(p.id)}>
                      Edit
                    </Button>
                    {confirmingDeleteId === p.id ? (
                      <>
                        <Button
                          variant="danger"
                          disabled={deleteMutation.isPending}
                          onClick={() => deleteMutation.mutate(p.id)}
                        >
                          Confirm delete
                        </Button>
                        <Button variant="secondary" onClick={() => setConfirmingDeleteId(null)}>
                          Cancel
                        </Button>
                      </>
                    ) : (
                      <Button variant="secondary" onClick={() => setConfirmingDeleteId(p.id)}>
                        Delete
                      </Button>
                    )}
                  </div>
                )}
              </li>
            ),
          )}
        </ul>
      ) : (
        <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">No parser profiles yet.</p>
      )}

      {!canManage && (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Only Owners and Finance officers can create or change parser profiles.
        </p>
      )}

      {canManage && (
      <form onSubmit={onSubmit} className="space-y-3" noValidate>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Profile name" htmlFor="profileName">
            <Input
              id="profileName"
              placeholder="Main bank"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label="Confidence threshold (0–1)" htmlFor="confidence">
            <Input
              id="confidence"
              type="number"
              min={0}
              max={1}
              step={0.05}
              required
              value={confidenceThreshold}
              onChange={(e) => setConfidenceThreshold(Number(e.target.value))}
            />
          </Field>
        </div>
        <Field label="Sender addresses/domains (comma-separated)" htmlFor="senderPatterns">
          <Input
            id="senderPatterns"
            placeholder="notifications@yourbank.com, @yourbank.com"
            required
            value={senderPatterns}
            onChange={(e) => setSenderPatterns(e.target.value)}
          />
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Use an exact address, or "@domain.com" to match any sender at that domain.
          </p>
        </Field>
        <ErrorText>{error}</ErrorText>
        <Button type="submit" disabled={createMutation.isPending}>
          {createMutation.isPending ? "Creating…" : "Add parser profile"}
        </Button>
      </form>
      )}
    </Card>
  );
}

export default function MailboxSettingsPage() {
  const { activeOrg } = useOrg();

  if (!activeOrg) return null;

  // Any active member can view; ConnectionsSection/ParserProfilesSection
  // each gate their own create/edit/delete controls to Owner/Finance.
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Mailbox &amp; parsing</h1>
      <ConnectionsSection />
      <ParserProfilesSection />
    </div>
  );
}
