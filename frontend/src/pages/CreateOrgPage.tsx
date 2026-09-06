import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { useOrg } from "@/auth/OrgContext";
import { ApiError } from "@/lib/api";
import { orgApi } from "@/lib/endpoints";
import { NONPROFIT_TYPES } from "@/lib/nonprofitTypes";
import { listTimezones } from "@/lib/timezones";
import { Button, Card, ErrorText, Field, Input, Label } from "@/components/ui";

const COUNTRIES = [
  { code: "CA", label: "Canada" },
  { code: "US", label: "United States" },
  { code: "GB", label: "United Kingdom" },
  { code: "AU", label: "Australia" },
];

const CURRENCIES = ["CAD", "USD", "GBP", "AUD", "EUR"];

const SELECT_CLASSES =
  "w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100";

export default function CreateOrgPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { setActiveOrgId, refetch } = useOrg();

  const timezones = useMemo(() => listTimezones(), []);

  const [name, setName] = useState("");
  const [legalName, setLegalName] = useState("");
  const [country, setCountry] = useState("CA");
  const [currency, setCurrency] = useState("CAD");
  const [timezone, setTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Toronto",
  );
  const [nonprofitTypes, setNonprofitTypes] = useState<string[]>([]);
  const [otherNonprofitType, setOtherNonprofitType] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const resolvedTypes = nonprofitTypes
        .filter((t) => t !== "Other")
        .concat(nonprofitTypes.includes("Other") && otherNonprofitType.trim() ? [otherNonprofitType.trim()] : []);
      const org = await orgApi.create({
        name,
        legal_name: legalName || undefined,
        country,
        currency,
        timezone,
        nonprofit_type: resolvedTypes.length > 0 ? resolvedTypes : undefined,
      });
      await refetch();
      setActiveOrgId(org.id);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the organization.");
    } finally {
      setSubmitting(false);
    }
  }

  if (user && !user.mfa_enabled) {
    return (
      <div className="mx-auto max-w-lg px-4 py-10">
        <Card>
          <h1 className="mb-1 text-xl font-semibold text-slate-900 dark:text-slate-100">Set up your organization</h1>
          <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
            Creating an organization makes you its Owner, and Owners must have two-factor
            authentication enabled first (spec 3.1).
          </p>
          <Link to="/mfa-setup">
            <Button>Set up two-factor authentication</Button>
          </Link>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-lg px-4 py-10">
      <Card>
        <h1 className="mb-1 text-xl font-semibold text-slate-900 dark:text-slate-100">Set up your organization</h1>
        <p className="mb-2 text-sm text-slate-500 dark:text-slate-400">
          You'll be its Owner. You can invite Finance and Media teammates afterward.
        </p>
        <p className="mb-6 text-sm text-slate-500 dark:text-slate-400">
          Joining an existing team instead?{" "}
          <Link to="/organizations/join" className="text-brand-600 hover:text-brand-700">
            Enter an organization code
          </Link>
          .
        </p>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <Field label="Organization name" htmlFor="name">
            <Input id="name" required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Legal name (optional)" htmlFor="legalName">
            <Input id="legalName" value={legalName} onChange={(e) => setLegalName(e.target.value)} />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <Label htmlFor="country">Country</Label>
              <select
                id="country"
                className={SELECT_CLASSES}
                value={country}
                onChange={(e) => setCountry(e.target.value)}
              >
                {COUNTRIES.map((c) => (
                  <option key={c.code} value={c.code}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <Label htmlFor="currency">Currency</Label>
              <select
                id="currency"
                className={SELECT_CLASSES}
                value={currency}
                onChange={(e) => setCurrency(e.target.value)}
              >
                {CURRENCIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <Label htmlFor="timezone">Time zone</Label>
            <select
              id="timezone"
              required
              className={SELECT_CLASSES}
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
            >
              {!timezones.includes(timezone) && <option value={timezone}>{timezone}</option>}
              {timezones.map((tz) => (
                <option key={tz} value={tz}>
                  {tz}
                </option>
              ))}
            </select>
          </div>
          <div>
            <Label htmlFor="nonprofitType">Nonprofit type (optional, select any that apply)</Label>
            <select
              id="nonprofitType"
              multiple
              size={5}
              className={SELECT_CLASSES}
              value={nonprofitTypes}
              onChange={(e) =>
                setNonprofitTypes(Array.from(e.target.selectedOptions, (o) => o.value))
              }
            >
              {NONPROFIT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              Hold Ctrl (or Cmd on Mac) to select more than one.
            </p>
            {nonprofitTypes.includes("Other") && (
              <Input
                className="mt-2"
                placeholder="Describe your nonprofit type"
                value={otherNonprofitType}
                onChange={(e) => setOtherNonprofitType(e.target.value)}
              />
            )}
          </div>
          <ErrorText>{error}</ErrorText>
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Creating…" : "Create organization"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
