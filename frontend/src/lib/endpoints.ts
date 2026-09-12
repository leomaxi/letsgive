import { api } from "./api";
import type {
  AdminOrganization,
  AdminOrganizationDetail,
  AdminSupportTicket,
  AdminSupportTicketDetail,
  AuditLogEntry,
  ContributionEvent,
  DisplayTemplate,
  DisplayTokenResponse,
  ElementType,
  ImapCheckNowResult,
  Invitation,
  JoinRequest,
  MailboxConnection,
  MfaEnrollResponse,
  Membership,
  MyOrganization,
  Notification,
  Organization,
  OperatorSocketTokenResponse,
  OrgJoinRequest,
  ParserProfile,
  Plan,
  ReconciliationItem,
  ReconciliationResolution,
  ReconciliationStatus,
  RecentImapMessage,
  RequestApprovalResponse,
  SessionOperator,
  SessionReport,
  SessionStatus,
  SubscriptionStatus,
  SupportTicket,
  SupportTicketDetail,
  SupportTicketStatus,
  TokenResponse,
  User,
} from "./types";

export const authApi = {
  register: (email: string, password: string, fullName: string) =>
    api.post<User>("/v1/auth/register", { email, password, full_name: fullName }, { auth: false }),

  login: (email: string, password: string, mfaCode?: string) =>
    api.post<TokenResponse>(
      "/v1/auth/login",
      { email, password, mfa_code: mfaCode || undefined },
      { auth: false }
    ),

  me: () => api.get<User>("/v1/auth/me"),

  updateEmail: (email: string, currentPassword: string) =>
    api.patch<User>("/v1/auth/me/email", { email, current_password: currentPassword }),

  enrollMfa: () => api.post<MfaEnrollResponse>("/v1/auth/mfa/enroll"),

  activateMfa: (code: string) => api.post<void>("/v1/auth/mfa/activate", { code }),
};

export const orgApi = {
  listMine: () => api.get<MyOrganization[]>("/v1/organizations"),

  create: (payload: {
    name: string;
    legal_name?: string;
    country: string;
    timezone: string;
    currency: string;
    nonprofit_type?: string[];
  }) => api.post<Organization>("/v1/organizations", payload),

  get: (orgId: string) => api.get<Organization>(`/v1/organizations/${orgId}`),

  listMembers: (orgId: string) => api.get<Membership[]>(`/v1/organizations/${orgId}/members`),

  inviteMember: (orgId: string, email: string, role: string) =>
    api.post<Membership>(`/v1/organizations/${orgId}/members/invite`, { email, role }),

  updateMemberRole: (orgId: string, memberId: string, role: string) =>
    api.patch<Membership>(`/v1/organizations/${orgId}/members/${memberId}`, { role }),

  cancelSubscription: (orgId: string) =>
    api.post<Organization>(`/v1/organizations/${orgId}/subscription/cancel`),

  switchPlan: (orgId: string, planId: string) =>
    api.post<Organization>(`/v1/organizations/${orgId}/subscription/switch-plan`, {
      plan_id: planId,
    }),
};

export const invitationApi = {
  list: () => api.get<Invitation[]>("/v1/me/invitations"),

  accept: (membershipId: string) =>
    api.post<Invitation>(`/v1/me/invitations/${membershipId}/accept`),

  decline: (membershipId: string) =>
    api.post<void>(`/v1/me/invitations/${membershipId}/decline`),
};

export const plansApi = {
  list: () => api.get<Plan[]>("/v1/plans"),
};

export const notificationApi = {
  list: (unreadOnly = false) =>
    api.get<Notification[]>(`/v1/me/notifications${unreadOnly ? "?unread_only=true" : ""}`),

  markRead: (notificationId: string) =>
    api.post<Notification>(`/v1/me/notifications/${notificationId}/read`),

  markAllRead: () => api.post<void>("/v1/me/notifications/read-all"),
};

export const joinRequestApi = {
  join: (joinCode: string) =>
    api.post<JoinRequest>("/v1/organizations/join", { join_code: joinCode }),

  listMine: () => api.get<JoinRequest[]>("/v1/me/join-requests"),

  cancelMine: (joinRequestId: string) =>
    api.post<void>(`/v1/me/join-requests/${joinRequestId}/cancel`),

  listForOrg: (orgId: string) =>
    api.get<OrgJoinRequest[]>(`/v1/organizations/${orgId}/join-requests`),

  approve: (orgId: string, joinRequestId: string, role: string) =>
    api.post<Membership>(`/v1/organizations/${orgId}/join-requests/${joinRequestId}/approve`, { role }),

  deny: (orgId: string, joinRequestId: string) =>
    api.post<void>(`/v1/organizations/${orgId}/join-requests/${joinRequestId}/deny`),
};

export interface CreateSessionPayload {
  organization_id: string;
  contribution_method: string;
  duration_seconds: number;
  mailbox_connection_id?: string;
  display_template_id?: string;
  goal_enabled?: boolean;
  goal_amount?: string;
  test_mode?: boolean;
}

export const sessionApi = {
  listForOrg: (orgId: string, status?: SessionStatus) =>
    api.get<SessionOperator[]>(
      `/v1/organizations/${orgId}/sessions${status ? `?status=${status}` : ""}`
    ),

  create: (payload: CreateSessionPayload) => api.post<SessionOperator>("/v1/sessions", payload),

  getOperator: (sessionId: string) => api.get<SessionOperator>(`/v1/sessions/${sessionId}/operator`),

  requestApproval: (sessionId: string) =>
    api.post<RequestApprovalResponse>(`/v1/sessions/${sessionId}/request-approval`),

  verify: (sessionId: string, approvalId: string, code: string) =>
    api.post<SessionOperator>(`/v1/sessions/${sessionId}/verify`, {
      approval_id: approvalId,
      code,
    }),

  start: (sessionId: string, expectedVersion: number) =>
    api.post<SessionOperator>(`/v1/sessions/${sessionId}/start`, {
      expected_version: expectedVersion,
    }),

  pause: (sessionId: string, expectedVersion: number) =>
    api.post<SessionOperator>(`/v1/sessions/${sessionId}/pause`, {
      expected_version: expectedVersion,
    }),

  resume: (sessionId: string, expectedVersion: number) =>
    api.post<SessionOperator>(`/v1/sessions/${sessionId}/resume`, {
      expected_version: expectedVersion,
    }),

  extend: (sessionId: string, expectedVersion: number, additionalSeconds: number) =>
    api.post<SessionOperator>(`/v1/sessions/${sessionId}/extend`, {
      expected_version: expectedVersion,
      additional_seconds: additionalSeconds,
    }),

  setVisibility: (sessionId: string, expectedVersion: number, amountVisible: boolean) =>
    api.patch<SessionOperator>(`/v1/sessions/${sessionId}/visibility`, {
      expected_version: expectedVersion,
      amount_visible: amountVisible,
    }),

  updateGoal: (sessionId: string, expectedVersion: number, goalAmount: string) =>
    api.patch<SessionOperator>(`/v1/sessions/${sessionId}/goal`, {
      expected_version: expectedVersion,
      goal_amount: goalAmount,
    }),

  close: (sessionId: string, expectedVersion: number) =>
    api.post<SessionOperator>(`/v1/sessions/${sessionId}/close`, {
      expected_version: expectedVersion,
    }),

  displayToken: (sessionId: string) =>
    api.post<DisplayTokenResponse>(`/v1/sessions/${sessionId}/display-token`),

  operatorSocketToken: (sessionId: string) =>
    api.post<OperatorSocketTokenResponse>(`/v1/sessions/${sessionId}/operator-socket-token`),

  events: (sessionId: string) => api.get<ContributionEvent[]>(`/v1/sessions/${sessionId}/events`),

  simulateDeposit: (sessionId: string, amount: string) =>
    api.post<ContributionEvent>(`/v1/sessions/${sessionId}/simulate-deposit`, { amount }),
};

export const connectionApi = {
  listForOrg: (orgId: string) => api.get<MailboxConnection[]>(`/v1/organizations/${orgId}/connections`),

  create: (
    orgId: string,
    payload: {
      provider: string;
      mailbox: string;
      folder?: string;
      imap_password?: string;
      imap_host?: string;
      imap_port?: number;
    },
  ) => api.post<MailboxConnection>(`/v1/organizations/${orgId}/connections`, payload),

  revoke: (orgId: string, connectionId: string) =>
    api.post<MailboxConnection>(`/v1/organizations/${orgId}/connections/${connectionId}/revoke`),

  checkNow: (orgId: string, connectionId: string) =>
    api.post<ImapCheckNowResult>(`/v1/organizations/${orgId}/connections/${connectionId}/check-now`),

  recentMessages: (orgId: string, connectionId: string) =>
    api.get<RecentImapMessage[]>(`/v1/organizations/${orgId}/connections/${connectionId}/recent-messages`),

  remove: (orgId: string, connectionId: string) =>
    api.delete<void>(`/v1/organizations/${orgId}/connections/${connectionId}`),
};

export interface CreateParserProfilePayload {
  name: string;
  sender_patterns: string[];
  template_version?: string;
  confidence_threshold?: number;
  default_currency?: string;
}

export interface UpdateParserProfilePayload {
  name?: string;
  sender_patterns?: string[];
  reject_keywords?: string[];
  confidence_threshold?: number;
  is_active?: boolean;
}

export const parserProfileApi = {
  listForOrg: (orgId: string) =>
    api.get<ParserProfile[]>(`/v1/organizations/${orgId}/parser-profiles`),

  create: (orgId: string, payload: CreateParserProfilePayload) =>
    api.post<ParserProfile>(`/v1/organizations/${orgId}/parser-profiles`, payload),

  update: (orgId: string, profileId: string, payload: UpdateParserProfilePayload) =>
    api.patch<ParserProfile>(`/v1/organizations/${orgId}/parser-profiles/${profileId}`, payload),

  remove: (orgId: string, profileId: string) =>
    api.delete<void>(`/v1/organizations/${orgId}/parser-profiles/${profileId}`),
};

export const reconciliationApi = {
  listForOrg: (orgId: string, status?: ReconciliationStatus) =>
    api.get<ReconciliationItem[]>(
      `/v1/organizations/${orgId}/reconciliation${status ? `?status_filter=${status}` : ""}`
    ),

  resolve: (
    itemId: string,
    resolution: ReconciliationResolution,
    correctedAmount?: string,
    note?: string,
    reversesEventId?: string
  ) =>
    api.post<ReconciliationItem>(`/v1/reconciliation/${itemId}/resolve`, {
      resolution,
      corrected_amount: correctedAmount || undefined,
      note: note || undefined,
      reverses_event_id: reversesEventId || undefined,
    }),
};

export const reportApi = {
  getSessionReport: (sessionId: string) =>
    api.get<SessionReport>(`/v1/reports/sessions/${sessionId}`),

  sessionCsvExportUrl: (sessionId: string) => `/v1/reports/sessions/${sessionId}/export.csv`,
  sessionPdfExportUrl: (sessionId: string) => `/v1/reports/sessions/${sessionId}/export.pdf`,
};

export interface DisplayElementInput {
  type: ElementType;
  x: number;
  y: number;
  width: number;
  height: number;
  z_index: number;
  style: Record<string, unknown>;
  binding: Record<string, unknown>;
  is_locked: boolean;
  is_hidden: boolean;
}

export interface SaveDisplayTemplatePayload {
  name: string;
  canvas?: { width: number; height: number; background_color: string };
  is_default: boolean;
  expected_version: number;
  elements: DisplayElementInput[];
}

export const displayTemplateApi = {
  listForOrg: (orgId: string) =>
    api.get<DisplayTemplate[]>(`/v1/organizations/${orgId}/display-templates`),

  get: (orgId: string, templateId: string) =>
    api.get<DisplayTemplate>(`/v1/organizations/${orgId}/display-templates/${templateId}`),

  create: (orgId: string) =>
    api.post<DisplayTemplate>(`/v1/organizations/${orgId}/display-templates`),

  save: (orgId: string, templateId: string, payload: SaveDisplayTemplatePayload) =>
    api.put<DisplayTemplate>(`/v1/organizations/${orgId}/display-templates/${templateId}`, payload),

  duplicate: (orgId: string, templateId: string) =>
    api.post<DisplayTemplate>(`/v1/organizations/${orgId}/display-templates/${templateId}/duplicate`),

  remove: (orgId: string, templateId: string) =>
    api.delete<void>(`/v1/organizations/${orgId}/display-templates/${templateId}`),
};

export const auditApi = {
  listForOrg: (orgId: string, limit = 100) =>
    api.get<AuditLogEntry[]>(`/v1/organizations/${orgId}/audit-logs?limit=${limit}`),
};

export const supportTicketApi = {
  listForOrg: (orgId: string) =>
    api.get<SupportTicket[]>(`/v1/organizations/${orgId}/support-tickets`),

  create: (orgId: string, subject: string, body: string) =>
    api.post<SupportTicketDetail>(`/v1/organizations/${orgId}/support-tickets`, { subject, body }),

  get: (orgId: string, ticketId: string) =>
    api.get<SupportTicketDetail>(`/v1/organizations/${orgId}/support-tickets/${ticketId}`),

  reply: (orgId: string, ticketId: string, body: string) =>
    api.post(`/v1/organizations/${orgId}/support-tickets/${ticketId}/messages`, { body }),
};

export interface AdminSubscriptionUpdatePayload {
  plan_id?: string;
  subscription_status?: SubscriptionStatus;
  plan_starts_at?: string;
  plan_expires_at?: string;
}

export const adminApi = {
  listOrganizations: (limit = 50, offset = 0, search?: string) =>
    api.get<AdminOrganization[]>(
      `/v1/admin/organizations?limit=${limit}&offset=${offset}${
        search ? `&search=${encodeURIComponent(search)}` : ""
      }`
    ),

  getOrganization: (orgId: string) =>
    api.get<AdminOrganizationDetail>(`/v1/admin/organizations/${orgId}`),

  updateSubscription: (orgId: string, payload: AdminSubscriptionUpdatePayload) =>
    api.post<AdminOrganizationDetail>(`/v1/admin/organizations/${orgId}/subscription`, payload),

  listTickets: (status?: SupportTicketStatus, organizationId?: string) => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (organizationId) params.set("organization_id", organizationId);
    const qs = params.toString();
    return api.get<AdminSupportTicket[]>(`/v1/admin/tickets${qs ? `?${qs}` : ""}`);
  },

  getTicket: (ticketId: string) =>
    api.get<AdminSupportTicketDetail>(`/v1/admin/tickets/${ticketId}`),

  replyToTicket: (ticketId: string, body: string) =>
    api.post(`/v1/admin/tickets/${ticketId}/messages`, { body }),

  setTicketStatus: (ticketId: string, status: SupportTicketStatus) =>
    api.post<AdminSupportTicket>(`/v1/admin/tickets/${ticketId}/status`, { status }),

  listAuditLogs: (organizationId?: string, limit = 100) =>
    api.get<AuditLogEntry[]>(
      `/v1/admin/audit-logs?limit=${limit}${organizationId ? `&organization_id=${organizationId}` : ""}`
    ),
};
