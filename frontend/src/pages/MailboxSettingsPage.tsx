import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { connectionApi, parserProfileApi } from "@/lib/endpoints";
import type { ConnectionStatus } from "@/lib/types";
import { Badge, Button, Card, ErrorText, Field, Input, Label, Spinner } from "@/components/ui";

const CONNECTION_STATUS_TONE: Record<ConnectionStatus, "slate" | "green" | "red" | "amber"> = {
  pending: "amber",
  connected: "green",
  error: "red",
  revoked: "slate",
};

function ConnectionsSection() {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();

  const [mailbox, setMailbox] = useState("");
  const [error, setError] = useState<string | null>(null);

  const connectionsQuery = useQuery({
    queryKey: ["connections", activeOrg?.id],
    queryFn: () => connectionApi.listForOrg(activeOrg!.id),
    enabled: !!activeOrg,
  });

  const createMutation = useMutation({
    mutationFn: () => connectionApi.create(activeOrg!.id, "fake", mailbox),
    onSuccess: () => {
      setMailbox("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["connections", activeOrg?.id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not add the mailbox."),
  });

  const revokeMutation = useMutation({
    mutationFn: (connectionId: string) => connectionApi.revoke(activeOrg!.id, connectionId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["connections", activeOrg?.id] }),
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
                  {c.last_sync_at && ` · last synced ${new Date(c.last_sync_at).toLocaleString()}`}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Badge tone={CONNECTION_STATUS_TONE[c.status]}>{c.status}</Badge>
                {c.status !== "revoked" && (
                  <Button
                    variant="secondary"
                    disabled={revokeMutation.isPending}
                    onClick={() => revokeMutation.mutate(c.id)}
                  >
                    Revoke
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">No mailbox connections yet.</p>
      )}

      <form onSubmit={onSubmit} className="flex items-end gap-2" noValidate>
        <div className="flex-1">
          <Label htmlFor="mailbox">Add a dedicated mailbox</Label>
          <Input
            id="mailbox"
            type="email"
            placeholder="deposits@yourchurch.org"
            required
            value={mailbox}
            onChange={(e) => setMailbox(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={createMutation.isPending}>
          {createMutation.isPending ? "Adding…" : "Add"}
        </Button>
      </form>
      <p className="mt-2 text-xs text-slate-400 dark:text-slate-500">
        Only the "fake" dev provider is wired up in this environment — real Microsoft 365/Gmail OAuth
        needs a registered app and live credentials this environment doesn't have (see README).
      </p>
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function ParserProfilesSection() {
  const { activeOrg } = useOrg();
  const queryClient = useQueryClient();

  const [name, setName] = useState("");
  const [senderPatterns, setSenderPatterns] = useState("");
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.75);
  const [error, setError] = useState<string | null>(null);

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
          {profilesQuery.data.map((p) => (
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
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">No parser profiles yet.</p>
      )}

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
    </Card>
  );
}

export default function MailboxSettingsPage() {
  const { activeOrg } = useOrg();

  if (!activeOrg) return null;

  if (activeOrg.role !== "owner" && activeOrg.role !== "finance") {
    return (
      <Card className="text-sm text-slate-500 dark:text-slate-400">
        Only Owners and Finance officers can manage mailbox connections and parser profiles.
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Mailbox &amp; parsing</h1>
      <ConnectionsSection />
      <ParserProfilesSection />
    </div>
  );
}
