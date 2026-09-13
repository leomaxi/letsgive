import { useMemo, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "@/lib/api";
import { authApi } from "@/lib/endpoints";
import { Button, Card, ErrorText, Field, Input } from "@/components/ui";
import BrandMark from "@/components/BrandMark";

export default function ResetPasswordPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const token = useMemo(() => new URLSearchParams(location.search).get("token") ?? "", [location.search]);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    try {
      await authApi.confirmPasswordReset(token, password);
      navigate("/login", {
        replace: true,
        state: { resetComplete: true },
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reset your password.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 dark:bg-slate-900">
      <Card className="w-full max-w-sm">
        <div className="mb-6 flex justify-center">
          <BrandMark size="lg" />
        </div>
        <h1 className="mb-2 text-xl font-semibold text-slate-900 dark:text-slate-100">Choose a new password</h1>
        <p className="mb-6 text-sm text-slate-500 dark:text-slate-400">
          Password reset links expire after 30 minutes and can only be used once.
        </p>

        {!token ? (
          <div className="space-y-4">
            <ErrorText>This reset link is missing its token. Request a new link from the sign-in page.</ErrorText>
            <Link to="/login">
              <Button className="w-full">Back to sign in</Button>
            </Link>
          </div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <Field label="New password" htmlFor="password">
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                minLength={10}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <Field label="Confirm password" htmlFor="confirmPassword">
              <Input
                id="confirmPassword"
                type="password"
                autoComplete="new-password"
                required
                minLength={10}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
            </Field>
            <ErrorText>{error}</ErrorText>
            <Button type="submit" className="w-full" disabled={submitting || password.length < 10}>
              {submitting ? "Updating…" : "Update password"}
            </Button>
          </form>
        )}
      </Card>
    </div>
  );
}
