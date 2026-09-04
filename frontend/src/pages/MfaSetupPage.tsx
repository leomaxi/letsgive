import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import QRCode from "qrcode";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/lib/api";
import { authApi } from "@/lib/endpoints";
import { Button, Card, ErrorText, Field, Input, Spinner } from "@/components/ui";
import BrandMark from "@/components/BrandMark";

export default function MfaSetupPage() {
  const { user, refreshUser } = useAuth();
  const navigate = useNavigate();
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const [secret, setSecret] = useState<string | null>(null);
  const [provisioningUri, setProvisioningUri] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (user?.mfa_enabled) {
      navigate("/", { replace: true });
      return;
    }
    authApi
      .enrollMfa()
      .then((res) => {
        setSecret(res.secret);
        setProvisioningUri(res.provisioning_uri);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Could not start MFA setup."))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (provisioningUri && canvasRef.current) {
      QRCode.toCanvas(canvasRef.current, provisioningUri, { width: 220, margin: 1 }).catch(() => {
        /* canvas render failure just falls back to the manual-entry secret below */
      });
    }
  }, [provisioningUri]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await authApi.activateMfa(code);
      await refreshUser();
      navigate("/", { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError ? "That code didn't match. Try the current code from your app." : "Something went wrong."
      );
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
        <h1 className="mb-1 text-xl font-semibold text-slate-900 dark:text-slate-100">Set up two-factor authentication</h1>
        <p className="mb-6 text-sm text-slate-500 dark:text-slate-400">
          Required before you can create or manage an organization (spec 3.1).
        </p>

        {loading ? (
          <div className="flex justify-center py-8">
            <Spinner className="h-6 w-6 text-brand-600" />
          </div>
        ) : (
          <>
            {provisioningUri && (
              <div className="mb-4 flex justify-center">
                <canvas ref={canvasRef} aria-label="Scan this QR code with your authenticator app" />
              </div>
            )}
            {secret && (
              <p className="mb-4 break-all rounded-md bg-slate-100 px-3 py-2 text-center font-mono text-xs text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                {secret}
              </p>
            )}
            <p className="mb-4 text-xs text-slate-500 dark:text-slate-400">
              Scan with Google Authenticator, 1Password, Authy or similar — or enter the code above
              manually — then enter the 6-digit code it shows.
            </p>
            <form onSubmit={onSubmit} className="space-y-4" noValidate>
              <Field label="Authenticator code" htmlFor="code">
                <Input
                  id="code"
                  inputMode="numeric"
                  pattern="[0-9]{6}"
                  maxLength={6}
                  autoFocus
                  required
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </Field>
              <ErrorText>{error}</ErrorText>
              <Button type="submit" className="w-full" disabled={submitting || !secret}>
                {submitting ? "Verifying…" : "Activate"}
              </Button>
            </form>
          </>
        )}
      </Card>
    </div>
  );
}
