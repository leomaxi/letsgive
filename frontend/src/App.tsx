import { lazy, Suspense } from "react";
import { Link, Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import RequireAuth from "@/auth/RequireAuth";
import { useOrg } from "@/auth/OrgContext";
import DashboardLayout from "@/components/DashboardLayout";
import InvitationsCard from "@/components/InvitationsCard";
import JoinRequestsPendingCard from "@/components/JoinRequestsPendingCard";
import { Card, Spinner } from "@/components/ui";
import { invitationApi, joinRequestApi } from "@/lib/endpoints";
// OrgHomePage is imported eagerly, not lazily like the routes below: it's
// rendered directly inside HomeRoute (not just as a route element), and
// it's the landing page nearly every authenticated session hits first
// anyway, so lazy-loading it would rarely save a real request -- the size
// win from code splitting is in the less-universally-visited pages.
import OrgHomePage from "@/pages/OrgHomePage";

const LoginPage = lazy(() => import("@/pages/LoginPage"));
const RegisterPage = lazy(() => import("@/pages/RegisterPage"));
const MfaSetupPage = lazy(() => import("@/pages/MfaSetupPage"));
const CreateOrgPage = lazy(() => import("@/pages/CreateOrgPage"));
const JoinOrganizationPage = lazy(() => import("@/pages/JoinOrganizationPage"));
const SessionsListPage = lazy(() => import("@/pages/SessionsListPage"));
const NewSessionPage = lazy(() => import("@/pages/NewSessionPage"));
const SessionDetailPage = lazy(() => import("@/pages/SessionDetailPage"));
const MailboxSettingsPage = lazy(() => import("@/pages/MailboxSettingsPage"));
const DisplayStudioListPage = lazy(() => import("@/pages/DisplayStudioListPage"));
const DisplayStudioEditorPage = lazy(() => import("@/pages/DisplayStudioEditorPage"));
const ReconciliationPage = lazy(() => import("@/pages/ReconciliationPage"));
const SessionReportPage = lazy(() => import("@/pages/SessionReportPage"));
const AuditLogPage = lazy(() => import("@/pages/AuditLogPage"));
const BillingPage = lazy(() => import("@/pages/BillingPage"));

function PageFallback() {
  return (
    <div className="flex justify-center py-16">
      <Spinner className="h-6 w-6 text-brand-600" />
    </div>
  );
}

export function HomeRoute() {
  const { organizations, isLoading, isFetching } = useOrg();
  const invitationsQuery = useQuery({
    queryKey: ["my-invitations"],
    queryFn: invitationApi.list,
  });
  const joinRequestsQuery = useQuery({
    queryKey: ["my-join-requests"],
    queryFn: joinRequestApi.listMine,
  });

  if (isLoading || invitationsQuery.isLoading || joinRequestsQuery.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    );
  }

  const hasInvitations = (invitationsQuery.data?.length ?? 0) > 0;
  const hasJoinRequests = (joinRequestsQuery.data?.length ?? 0) > 0;

  if (organizations.length === 0 && !hasInvitations && !hasJoinRequests) {
    // Accepting/declining an invitation invalidates both this org list and
    // the invitations list, and the two refetches can land a render apart.
    // Wait for all three to settle before trusting "nothing at all" enough
    // to redirect -- otherwise a stale frame bounces the user to
    // /organizations/new right after they accepted or requested something.
    // This check is scoped to just this branch (not the whole component) so
    // it can't turn into a mount/refetch loop with the cards below.
    if (isFetching || invitationsQuery.isFetching || joinRequestsQuery.isFetching) {
      return (
        <div className="flex justify-center py-16">
          <Spinner className="h-6 w-6 text-brand-600" />
        </div>
      );
    }
    return <Navigate to="/organizations/new" replace />;
  }

  return (
    <div className="space-y-6">
      <InvitationsCard />
      <JoinRequestsPendingCard />
      {organizations.length > 0 ? (
        <OrgHomePage />
      ) : (
        <Card className="text-sm text-slate-500 dark:text-slate-400">
          You don't belong to an organization yet. Accept an invitation above, or{" "}
          <Link to="/organizations/new" className="text-brand-600 hover:text-brand-700">
            create a new one
          </Link>
          .
        </Card>
      )}
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<PageFallback />}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />

        <Route element={<RequireAuth />}>
          <Route path="/mfa-setup" element={<MfaSetupPage />} />

          <Route element={<DashboardLayout />}>
            <Route path="/" element={<HomeRoute />} />
            <Route path="/organizations/new" element={<CreateOrgPage />} />
            <Route path="/organizations/join" element={<JoinOrganizationPage />} />
            <Route path="/sessions" element={<SessionsListPage />} />
            <Route path="/sessions/new" element={<NewSessionPage />} />
            <Route path="/sessions/:sessionId" element={<SessionDetailPage />} />
            <Route path="/mailbox" element={<MailboxSettingsPage />} />
            <Route path="/display-studio" element={<DisplayStudioListPage />} />
            <Route path="/display-studio/:templateId" element={<DisplayStudioEditorPage />} />
            <Route path="/reconciliation" element={<ReconciliationPage />} />
            <Route path="/sessions/:sessionId/report" element={<SessionReportPage />} />
            <Route path="/audit" element={<AuditLogPage />} />
            <Route path="/billing" element={<BillingPage />} />
          </Route>
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
