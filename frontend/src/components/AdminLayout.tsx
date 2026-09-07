import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useTheme } from "@/auth/ThemeContext";
import { Badge } from "@/components/ui";
import BrandMark from "@/components/BrandMark";
import { adminApi } from "@/lib/endpoints";

// Deliberately its own layout, not nested inside DashboardLayout -- a
// platform-admin account is not scoped to any single tenant org (no org
// switcher, no tenant nav, no NotificationsBell -- see AdminTicketsPage for
// why the admin side doesn't need the tenant Notification mechanism at all).
const NAV_ITEMS = [
  { to: "/admin/organizations", label: "Organizations" },
  { to: "/admin/tickets", label: "Tickets" },
] as const;

export default function AdminLayout() {
  const { user, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();

  const unreadTicketsQuery = useQuery({
    queryKey: ["admin-tickets-unread"],
    queryFn: () => adminApi.listTickets(),
    refetchInterval: 10000,
  });
  const unreadCount = unreadTicketsQuery.data?.filter((t) => t.admin_unread).length ?? 0;

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900">
      <header className="border-b border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 overflow-x-auto px-4 py-3">
          <div className="flex flex-shrink-0 items-center gap-6">
            <BrandMark className="flex-shrink-0" />
            <Badge tone="blue">
              <span className="whitespace-nowrap">platform admin</span>
            </Badge>
          </div>
          <div className="flex flex-shrink-0 items-center gap-4">
            <button
              onClick={toggleTheme}
              aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
              className="flex-shrink-0 rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-100"
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
            <span className="max-w-[10rem] truncate text-sm text-slate-500 dark:text-slate-400" title={user?.email}>
              {user?.email}
            </span>
            <button
              onClick={logout}
              className="flex-shrink-0 whitespace-nowrap text-sm font-medium text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-slate-100"
            >
              Sign out
            </button>
          </div>
        </div>
        <nav className="mx-auto flex max-w-6xl gap-1 overflow-x-auto px-4">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `flex-shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${
                  isActive
                    ? "border-brand-600 text-brand-700 dark:border-brand-400 dark:text-brand-400"
                    : "border-transparent text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
                }`
              }
            >
              {item.label}
              {item.to === "/admin/tickets" && unreadCount > 0 && (
                <span className="ml-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-brand-600 px-1 text-[10px] font-semibold text-white">
                  {unreadCount}
                </span>
              )}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
