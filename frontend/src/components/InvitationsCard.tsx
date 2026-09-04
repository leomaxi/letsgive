import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { invitationApi } from "@/lib/endpoints";
import { Badge, Button, Card, ErrorText, Spinner } from "@/components/ui";

export default function InvitationsCard() {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["my-invitations"],
    queryFn: invitationApi.list,
  });

  async function respond(membershipId: string, action: "accept" | "decline") {
    setError(null);
    setBusyId(membershipId);
    try {
      if (action === "accept") {
        await invitationApi.accept(membershipId);
      } else {
        await invitationApi.decline(membershipId);
      }
      // Order matters: settle the org list before the invitations list so a
      // reader (HomeRoute) never observes "no orgs yet, no invites either".
      await queryClient.invalidateQueries({ queryKey: ["my-organizations"] });
      await queryClient.invalidateQueries({ queryKey: ["my-invitations"] });
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
        Pending invitations
      </h2>
      <ErrorText>{error}</ErrorText>
      <ul className="divide-y divide-slate-100 dark:divide-slate-700">
        {query.data.map((inv) => (
          <li key={inv.id} className="flex items-center justify-between py-3 text-sm">
            <div>
              <div className="font-medium text-slate-700 dark:text-slate-300">{inv.organization_name}</div>
              <div className="mt-1">
                <Badge tone="amber">Invited as {inv.role}</Badge>
              </div>
            </div>
            <div className="flex gap-2">
              <Button disabled={busyId === inv.id} onClick={() => respond(inv.id, "accept")}>
                Accept
              </Button>
              <Button
                variant="secondary"
                disabled={busyId === inv.id}
                onClick={() => respond(inv.id, "decline")}
              >
                Decline
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </Card>
  );
}
