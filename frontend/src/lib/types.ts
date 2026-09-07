export type Role = "owner" | "finance" | "media" | "auditor" | "system_admin";
export type MembershipStatus = "invited" | "active" | "suspended" | "requested";
export type SubscriptionStatus = "trialing" | "active" | "past_due" | "canceled";

export interface User {
  id: string;
  email: string;
  full_name: string;
  mfa_enabled: boolean;
  is_platform_admin: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface MfaEnrollResponse {
  secret: string;
  provisioning_uri: string;
}

export interface Organization {
  id: string;
  name: string;
  legal_name: string | null;
  country: string;
  timezone: string;
  currency: string;
  nonprofit_type: string[] | null;
  join_code: string;
  status: string;
  plan_id: string | null;
  subscription_status: SubscriptionStatus;
  grace_period_ends_at: string | null;
}

export interface MyOrganization extends Organization {
  role: Role;
  membership_status: MembershipStatus;
}

export interface Invitation {
  id: string;
  organization_id: string;
  organization_name: string;
  role: Role;
  status: MembershipStatus;
  created_at: string;
}

export type NotificationType = "approval_code" | "support_reply";

export interface Notification {
  id: string;
  organization_id: string;
  organization_name: string;
  session_id: string | null;
  type: NotificationType;
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
}

export interface JoinRequest {
  id: string;
  organization_id: string;
  organization_name: string;
  status: MembershipStatus;
  created_at: string;
}

export interface OrgJoinRequest {
  id: string;
  organization_id: string;
  user_id: string;
  user_email: string;
  user_full_name: string;
  created_at: string;
}

export interface Membership {
  id: string;
  user_id: string;
  user_email: string;
  user_full_name: string;
  organization_id: string;
  role: Role;
  status: MembershipStatus;
  created_at: string;
}

export interface Plan {
  id: string;
  key: string;
  name: string;
  max_sessions_per_month: number;
  max_mailbox_connections: number;
  max_display_templates: number;
  max_team_members: number;
  allows_custom_subdomain: boolean;
  allows_sso: boolean;
}

export interface ApiErrorBody {
  detail?: string | { msg: string }[];
}

export type SessionStatus =
  | "draft"
  | "approval_requested"
  | "authorized"
  | "live"
  | "paused"
  | "ended"
  | "reconciling"
  | "closed";

export interface SessionOperator {
  id: string;
  organization_id: string;
  mailbox_connection_id: string | null;
  display_template_id: string | null;
  status: SessionStatus;
  version: number;
  contribution_method: string;
  currency: string;
  duration_seconds: number;
  starts_at: string | null;
  ends_at: string | null;
  watermark: string | null;
  goal_enabled: boolean;
  goal_amount: string | null;
  amount_visible: boolean;
  test_mode: boolean;
  operator_warning: string | null;
  contribution_count: number;
  total_amount: string | null;
}

export type ContributionDecision =
  | "accepted"
  | "ambiguous"
  | "excluded_time_window"
  | "excluded_source_mismatch"
  | "excluded_no_credit_intent"
  | "excluded_no_active_session"
  | "reversed";

export interface ContributionEvent {
  id: string;
  session_id: string | null;
  provider_message_id: string;
  received_at: string;
  decision: ContributionDecision;
  decision_reason: string | null;
  amount: string | null;
  currency: string | null;
  parser_confidence: number | null;
  template_version: string | null;
  test_mode: boolean;
  corrects_event_id: string | null;
}

export interface RequestApprovalResponse {
  approval_id: string;
  expires_at: string;
  sent_to: string[];
}

export interface DisplayTokenResponse {
  display_token: string;
  expires_in_minutes: number;
}

export interface OperatorSocketTokenResponse {
  operator_socket_token: string;
  expires_in_minutes: number;
}

export type MailboxProvider = "fake" | "microsoft" | "gmail" | "imap";
export type ConnectionStatus = "pending" | "connected" | "error" | "revoked";

export interface MailboxConnection {
  id: string;
  organization_id: string;
  provider: MailboxProvider;
  mailbox: string;
  folder: string | null;
  status: ConnectionStatus;
  last_sync_at: string | null;
  webhook_health: string | null;
  imap_host: string | null;
  imap_port: number | null;
}

export interface ImapCheckNowResult {
  fetched: number;
  accepted: number;
  error: string | null;
}

export interface RecentImapMessage {
  uid: string;
  fetched_at: string;
  received_at: string;
  sender: string;
  subject: string;
  body_snippet: string;
  decision: string;
  decision_reason: string | null;
}

export interface ParserProfile {
  id: string;
  organization_id: string;
  name: string;
  template_version: string;
  sender_patterns: string[];
  credit_keywords: string[];
  reject_keywords: string[];
  amount_pattern: string;
  default_currency: string;
  confidence_threshold: number;
  is_active: boolean;
}

export type ReconciliationStatus = "pending" | "resolved";
export type ReconciliationResolution = "accepted" | "excluded" | "reversed" | "duplicate";

export interface ReconciliationItem {
  id: string;
  organization_id: string;
  session_id: string;
  contribution_event_id: string;
  reason: string;
  status: ReconciliationStatus;
  resolution: ReconciliationResolution | null;
  resolver_user_id: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  corrected_amount: string | null;
  evidence: Record<string, unknown>;
}

export interface SessionReport {
  session_id: string;
  status: SessionStatus;
  validated_count: number;
  validated_amount: string | null;
  excluded_counts: Record<string, number>;
  corrections: ReconciliationItem[];
  approval_history: {
    id: string;
    requested_by_user_id: string;
    created_at: string;
    expires_at: string;
    attempts: number;
    locked: boolean;
    verified_at: string | null;
  }[];
  connection_health: {
    connection_id: string;
    status: string;
    webhook_health: string | null;
    last_sync_at: string | null;
  } | null;
}

export type ElementType =
  | "logo"
  | "heading"
  | "body_text"
  | "contribution_count"
  | "amount"
  | "goal"
  | "progress_bar"
  | "countdown"
  | "payment_instructions"
  | "qr_code"
  | "background"
  | "sponsor_message";

export interface DisplayCanvas {
  width: number;
  height: number;
  background_color: string;
}

export interface DisplayElement {
  id: string;
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

export interface DisplayTemplate {
  id: string;
  organization_id: string;
  name: string;
  canvas: DisplayCanvas;
  is_default: boolean;
  version: number;
  elements: DisplayElement[];
}

export interface AuditLogEntry {
  id: string;
  organization_id: string | null;
  actor_user_id: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  ip_address: string | null;
  created_at: string;
}

export type SupportTicketStatus = "open" | "in_progress" | "resolved" | "closed";

export interface SupportTicketMessage {
  id: string;
  ticket_id: string;
  author_user_id: string;
  author_is_admin: boolean;
  body: string;
  created_at: string;
}

export interface SupportTicket {
  id: string;
  organization_id: string;
  created_by_user_id: string;
  subject: string;
  status: SupportTicketStatus;
  created_at: string;
  updated_at: string;
}

export interface SupportTicketDetail extends SupportTicket {
  messages: SupportTicketMessage[];
}

export interface AdminSupportTicket extends SupportTicket {
  organization_name: string;
  admin_unread: boolean;
}

export interface AdminSupportTicketDetail extends AdminSupportTicket {
  messages: SupportTicketMessage[];
}

export interface AdminOrganization {
  id: string;
  name: string;
  plan_id: string | null;
  plan_key: string | null;
  plan_name: string | null;
  subscription_status: SubscriptionStatus;
  member_count: number;
  created_at: string;
}

export interface AdminOrganizationDetail extends AdminOrganization {
  grace_period_ends_at: string | null;
  connections_total: number;
  connections_connected: number;
}
