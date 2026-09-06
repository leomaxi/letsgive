import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { joinRequestApi } from "@/lib/endpoints";
import { Button, Card, ErrorText, Field, Input } from "@/components/ui";

export default function JoinOrganizationPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [joinCode, setJoinCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await joinRequestApi.join(joinCode.trim());
      await queryClient.invalidateQueries({ queryKey: ["my-join-requests"] });
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not submit that code.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-lg px-4 py-10">
      <Card>
        <h1 className="mb-1 text-xl font-semibold text-slate-900 dark:text-slate-100">
          Join an organization
        </h1>
        <p className="mb-6 text-sm text-slate-500 dark:text-slate-400">
          Enter the organization code a teammate shared with you. The Owner will review your
          request and assign you a role before you get access.
        </p>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <Field label="Organization code" htmlFor="joinCode">
            <Input
              id="joinCode"
              required
              autoFocus
              placeholder="e.g. AB2CD3EF"
              value={joinCode}
              onChange={(e) => setJoinCode(e.target.value.toUpperCase())}
              className="text-center text-lg tracking-widest"
            />
          </Field>
          <ErrorText>{error}</ErrorText>
          <Button type="submit" className="w-full" disabled={submitting || !joinCode.trim()}>
            {submitting ? "Requesting…" : "Request to join"}
          </Button>
        </form>
        <p className="mt-4 text-center text-sm text-slate-500 dark:text-slate-400">
          Don't have a code?{" "}
          <Link to="/organizations/new" className="text-brand-600 hover:text-brand-700">
            Create a new organization
          </Link>{" "}
          instead.
        </p>
      </Card>
    </div>
  );
}
