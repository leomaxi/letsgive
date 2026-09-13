import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/lib/api";
import { authApi } from "@/lib/endpoints";
import { Button, Card, ErrorText, Field, Input } from "@/components/ui";
import BrandMark from "@/components/BrandMark";

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const justRegistered = (location.state as { justRegistered?: boolean } | null)?.justRegistered;
  const resetComplete = (location.state as { resetComplete?: boolean } | null)?.resetComplete;

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [needsMfa, setNeedsMfa] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resetMessage, setResetMessage] = useState<string | null>(null);
  const [showReset, setShowReset] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password, needsMfa ? mfaCode : undefined);
      navigate("/", { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.message === "mfa_required") {
        setNeedsMfa(true);
      } else if (err instanceof ApiError && err.message === "mfa_invalid") {
        setError("That code didn't match. Check your authenticator app and try again.");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function onResetRequest(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setResetMessage(null);
    setSubmitting(true);
    try {
      const res = await authApi.requestPasswordReset(email);
      setResetMessage(res.message);
      setShowReset(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start password reset.");
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
        <h1 className="mb-6 text-xl font-semibold text-slate-900 dark:text-slate-100">
          {showReset ? "Reset password" : "Sign in"}
        </h1>
        {justRegistered && !needsMfa && (
          <p className="mb-4 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
            Account created. Sign in to continue.
          </p>
        )}
        {resetComplete && !needsMfa && (
          <p className="mb-4 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
            Password updated. Sign in with your new password.
          </p>
        )}
        {resetMessage && (
          <p className="mb-4 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
            {resetMessage}
          </p>
        )}
        {showReset ? (
        <form onSubmit={onResetRequest} className="space-y-4" noValidate>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Enter your account email and we'll send a secure link to choose a new password.
          </p>
          <Field label="Email" htmlFor="resetEmail">
            <Input
              id="resetEmail"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <ErrorText>{error}</ErrorText>
          <Button type="submit" className="w-full" disabled={submitting || !email}>
            {submitting ? "Sending…" : "Send reset link"}
          </Button>
          <button
            type="button"
            onClick={() => {
              setShowReset(false);
              setError(null);
            }}
            className="w-full text-center text-sm font-medium text-brand-600 hover:text-brand-700"
          >
            Back to sign in
          </button>
        </form>
        ) : (
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <Field label="Email" htmlFor="email">
            <Input
              id="email"
              type="email"
              autoComplete="email"
              required
              disabled={needsMfa}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label="Password" htmlFor="password">
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              disabled={needsMfa}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          {!needsMfa && (
            <div className="text-right">
              <button
                type="button"
                onClick={() => {
                  setShowReset(true);
                  setError(null);
                  setResetMessage(null);
                }}
                className="text-sm font-medium text-brand-600 hover:text-brand-700"
              >
                Forgot password?
              </button>
            </div>
          )}
          {needsMfa && (
            <Field label="Authenticator code" htmlFor="mfaCode">
              <Input
                id="mfaCode"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                autoFocus
                required
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value)}
              />
            </Field>
          )}
          <ErrorText>{error}</ErrorText>
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Signing in…" : needsMfa ? "Verify and sign in" : "Sign in"}
          </Button>
        </form>
        )}
        <p className="mt-6 text-center text-sm text-slate-500 dark:text-slate-400">
          Need an account?{" "}
          <Link to="/register" className="font-medium text-brand-600 hover:text-brand-700">
            Create one
          </Link>
        </p>
      </Card>
    </div>
  );
}
