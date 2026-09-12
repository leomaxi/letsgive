import { useState, type FormEvent } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { useOrg } from "@/auth/OrgContext";
import { useTheme } from "@/auth/ThemeContext";
import { ApiError } from "@/lib/api";
import { authApi } from "@/lib/endpoints";
import { Badge, Button, ErrorText, Input, Select } from "@/components/ui";
import BrandMark from "@/components/BrandMark";
import NotificationsBell from "@/components/NotificationsBell";

// Every read-only page below is now open to any active member (the team
// can see sessions/logs/templates/mailbox config, not just Owner/Finance) --
// each page gates its own write controls (add/edit/delete/resolve) to the
// roles that can actually perform them. Only Billing stays fully restricted.
const NAV_ITEMS = [
  { to: "/", label: "Overview", end: true, roles: null },
  { to: "/sessions", label: "Sessions", end: false, roles: null },
  { to: "/mailbox", label: "Mailbox & parsing", end: false, roles: null },
  { to: "/display-studio", label: "Display Studio", end: false, roles: null },
  { to: "/reconciliation", label: "Reconciliation", end: false, roles: null },
  { to: "/audit", label: "Audit log", end: false, roles: null },
  { to: "/support", label: "Support", end: false, roles: null },
  { to: "/billing", label: "Billing", end: false, roles: ["owner", "finance"] },
] as const;

export default function DashboardLayout() {
  const { user, logout, refreshUser } = useAuth();
  const { organizations, activeOrg, setActiveOrgId } = useOrg();
  const { theme, toggleTheme } = useTheme();
  const [accountOpen, setAccountOpen] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [accountError, setAccountError] = useState<string | null>(null);
  const [accountSuccess, setAccountSuccess] = useState<string | null>(null);
  const [accountSaving, setAccountSaving] = useState(false);
  const visibleNavItems = NAV_ITEMS.filter(
    (item) => !item.roles || (activeOrg && (item.roles as readonly string[]).includes(activeOrg.role))
  );

  async function onChangeEmail(e: FormEvent) {
    e.preventDefault();
    setAccountError(null);
    setAccountSuccess(null);
    setAccountSaving(true);
    try {
      await authApi.updateEmail(newEmail, currentPassword);
      await refreshUser();
      setNewEmail("");
      setCurrentPassword("");
      setAccountSuccess("Email updated.");
    } catch (err) {
      setAccountError(err instanceof ApiError ? err.message : "Could not update email.");
    } finally {
      setAccountSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-brand-700 focus:shadow-lg dark:focus:bg-slate-800 dark:focus:text-brand-300"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-700 dark:bg-slate-800/95">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex min-w-0 flex-wrap items-center gap-4">
            <BrandMark className="flex-shrink-0" />
            {organizations.length > 0 && (
              <label className="min-w-0 flex-1 sm:flex-none">
                <span className="sr-only">Active organization</span>
                <Select
                  className="min-w-48 max-w-full py-1.5"
                  value={activeOrg?.id ?? ""}
                  onChange={(e) => setActiveOrgId(e.target.value)}
                >
                  {organizations.map((org) => (
                    <option key={org.id} value={org.id}>
                      {org.name}
                    </option>
                  ))}
                </Select>
              </label>
            )}
            {activeOrg && (
              <Badge tone="blue">
                <span className="whitespace-nowrap">{activeOrg.role}</span>
              </Badge>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3 lg:justify-end">
            {user?.is_platform_admin && (
              <Link
                to="/admin/organizations"
                className="flex-shrink-0 whitespace-nowrap text-sm font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
              >
                Admin portal
              </Link>
            )}
            <NotificationsBell />
            <button
              onClick={toggleTheme}
              aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              className="flex-shrink-0 rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-100"
            >
              {theme === "dark" ? (
                <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="12" cy="12" r="4" />
                  <path
                    strokeLinecap="round"
                    d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"
                  />
                </svg>
              ) : (
                <svg className="h-5 w-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
                </svg>
              )}
            </button>
            <div className="relative">
              <button
                type="button"
                onClick={() => {
                  setAccountOpen((open) => !open);
                  setAccountError(null);
                  setAccountSuccess(null);
                  setNewEmail(user?.email ?? "");
                }}
                className="max-w-[10rem] truncate text-sm text-slate-500 hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 dark:text-slate-400 dark:hover:text-slate-100"
                title={user?.email}
              >
                {user?.email}
              </button>
              {accountOpen && (
                <div className="absolute right-0 top-8 z-40 w-80 rounded-lg border border-slate-200 bg-white p-4 shadow-lg dark:border-slate-700 dark:bg-slate-800">
                  <form onSubmit={onChangeEmail} className="space-y-3" noValidate>
                    <div>
                      <label htmlFor="accountEmail" className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300">
                        Email
                      </label>
                      <Input
                        id="accountEmail"
                        type="email"
                        required
                        value={newEmail}
                        onChange={(e) => setNewEmail(e.target.value)}
                      />
                    </div>
                    <div>
                      <label htmlFor="accountPassword" className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300">
                        Current password
                      </label>
                      <Input
                        id="accountPassword"
                        type="password"
                        required
                        value={currentPassword}
                        onChange={(e) => setCurrentPassword(e.target.value)}
                      />
                    </div>
                    <ErrorText>{accountError}</ErrorText>
                    {accountSuccess && (
                      <p className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                        {accountSuccess}
                      </p>
                    )}
                    <div className="flex justify-end gap-2">
                      <Button type="button" variant="secondary" onClick={() => setAccountOpen(false)}>
                        Close
                      </Button>
                      <Button type="submit" disabled={accountSaving || !newEmail || !currentPassword}>
                        {accountSaving ? "Saving..." : "Save"}
                      </Button>
                    </div>
                  </form>
                </div>
              )}
            </div>
            <button
              onClick={logout}
              className="flex-shrink-0 whitespace-nowrap text-sm font-medium text-slate-600 hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 dark:text-slate-300 dark:hover:text-slate-100"
            >
              Sign out
            </button>
          </div>
        </div>
        {activeOrg && (
          <nav className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4" aria-label="Primary">
            {visibleNavItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `flex-shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${
                    isActive
                      ? "border-brand-600 text-brand-700 dark:border-brand-400 dark:text-brand-400"
                      : "border-transparent text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        )}
      </header>
      <main id="main-content" className="mx-auto max-w-7xl px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
