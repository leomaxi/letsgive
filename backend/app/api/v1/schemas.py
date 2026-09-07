from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.db.models.contribution_event import ContributionDecision
from app.db.models.display_template import ElementType
from app.db.models.mailbox_connection import ConnectionStatus, MailboxProviderName
from app.db.models.membership import MembershipStatus, Role
from app.db.models.notification import NotificationType
from app.db.models.organization import SubscriptionStatus
from app.db.models.reconciliation_item import ReconciliationResolution, ReconciliationStatus
from app.db.models.session import SessionStatus
from app.db.models.support_ticket import SupportTicketStatus


class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10)
    full_name: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    mfa_enabled: bool
    is_platform_admin: bool

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    mfa_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MfaEnrollResponse(BaseModel):
    secret: str
    provisioning_uri: str


class MfaActivateRequest(BaseModel):
    code: str


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = None
    country: str = Field(min_length=2, max_length=2)
    timezone: str
    currency: str = Field(min_length=3, max_length=3)
    nonprofit_type: list[str] | None = None


class OrganizationOut(BaseModel):
    id: str
    name: str
    legal_name: str | None
    country: str
    timezone: str
    currency: str
    nonprofit_type: list[str] | None
    join_code: str
    status: str
    plan_id: str | None
    subscription_status: SubscriptionStatus
    grace_period_ends_at: datetime | None

    model_config = {"from_attributes": True}

    @field_validator("nonprofit_type", mode="before")
    @classmethod
    def _split_nonprofit_type(cls, value: object) -> list[str] | None:
        # The ORM stores this comma-joined (see Organization.nonprofit_type);
        # a list value (e.g. round-tripping an already-validated model) is
        # passed through unchanged.
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            return parts or None
        return value  # type: ignore[return-value]


class MyOrganizationOut(OrganizationOut):
    """OrganizationOut plus the current user's role in it -- what a client
    needs to build an org switcher without a second request per org.
    """

    role: Role
    membership_status: MembershipStatus


class SwitchPlanRequest(BaseModel):
    plan_id: str


class MemberInviteRequest(BaseModel):
    email: EmailStr
    role: Role


class MembershipOut(BaseModel):
    id: str
    user_id: str
    user_email: EmailStr
    user_full_name: str
    organization_id: str
    role: Role
    status: MembershipStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class InvitationOut(BaseModel):
    """A pending (or just-resolved) membership from the invitee's own point
    of view -- organization_name is what a client needs to render the
    invite without a second request per organization.
    """

    id: str
    organization_id: str
    organization_name: str
    role: Role
    status: MembershipStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationOut(BaseModel):
    id: str
    organization_id: str
    organization_name: str
    session_id: str | None
    type: NotificationType
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class JoinOrganizationRequest(BaseModel):
    join_code: str = Field(min_length=1, max_length=20)


class JoinRequestOut(BaseModel):
    """A pending join-code request, from the requester's own point of view."""

    id: str
    organization_id: str
    organization_name: str
    status: MembershipStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class OrgJoinRequestOut(BaseModel):
    """A pending join-code request, from the Owner's point of view -- who is
    asking to join and with what account, so the Owner can decide.
    """

    id: str
    organization_id: str
    user_id: str
    user_email: EmailStr
    user_full_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ApproveJoinRequestRequest(BaseModel):
    role: Role


class SessionCreateRequest(BaseModel):
    organization_id: str
    contribution_method: str = Field(min_length=1, max_length=50)
    duration_seconds: int = Field(gt=0, le=6 * 60 * 60)
    display_template_id: str | None = None
    goal_enabled: bool = False
    goal_amount: Decimal | None = Field(default=None, ge=0)
    test_mode: bool = False
    mailbox_connection_id: str | None = None


class SessionOperatorOut(BaseModel):
    id: str
    organization_id: str
    mailbox_connection_id: str | None
    display_template_id: str | None
    status: SessionStatus
    version: int
    contribution_method: str
    currency: str
    duration_seconds: int
    starts_at: datetime | None
    ends_at: datetime | None
    watermark: datetime | None
    goal_enabled: bool
    goal_amount: Decimal | None
    amount_visible: bool
    test_mode: bool
    operator_warning: str | None
    contribution_count: int = 0
    total_amount: Decimal | None = None

    model_config = {"from_attributes": True}


class SessionPublicOut(BaseModel):
    status: SessionStatus
    organization_name: str
    currency: str
    ends_at: datetime | None
    goal_amount: Decimal | None = None
    amount_visible: bool
    contribution_count: int = 0
    total_amount: Decimal | None = None
    # Computed server-side from the real total, independent of
    # amount_visible -- a binary "did we hit it" milestone reveals far less
    # than the running total/progress bar does, so it's shown on the public
    # projection page even when the org has chosen to keep the exact amount
    # private. See app/api/v1/sessions.py::_public_payload.
    goal_reached: bool = False


class RequestApprovalResponse(BaseModel):
    approval_id: str
    expires_at: datetime
    sent_to: list[EmailStr]


class VerifyApprovalRequest(BaseModel):
    approval_id: str
    code: str = Field(min_length=6, max_length=6)


class ExpectedVersionRequest(BaseModel):
    expected_version: int


class ExtendSessionRequest(ExpectedVersionRequest):
    additional_seconds: int = Field(gt=0, le=2 * 60 * 60)


class VisibilityRequest(ExpectedVersionRequest):
    amount_visible: bool


class UpdateGoalRequest(ExpectedVersionRequest):
    goal_amount: Decimal = Field(gt=0)


class DisplayTokenResponse(BaseModel):
    display_token: str
    expires_in_minutes: int


class OperatorSocketTokenResponse(BaseModel):
    operator_socket_token: str
    expires_in_minutes: int


class SimulateDepositRequest(BaseModel):
    amount: Decimal = Field(gt=0)


class ContributionEventOut(BaseModel):
    id: str
    session_id: str | None
    provider_message_id: str
    received_at: datetime
    decision: ContributionDecision
    decision_reason: str | None
    amount: Decimal | None
    currency: str | None
    parser_confidence: float | None
    template_version: str | None
    test_mode: bool
    corrects_event_id: str | None

    model_config = {"from_attributes": True}


class MailboxConnectionCreateRequest(BaseModel):
    provider: MailboxProviderName
    mailbox: EmailStr
    folder: str | None = None
    # IMAP ("log in with your email") only. imap_host/imap_port can be
    # omitted for well-known providers (Gmail/Outlook/Yahoo/iCloud) -- see
    # app/domain/imap_provider.py's guess_imap_host.
    imap_password: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None


class MailboxConnectionOut(BaseModel):
    id: str
    organization_id: str
    provider: MailboxProviderName
    mailbox: str
    folder: str | None
    status: ConnectionStatus
    last_sync_at: datetime | None
    webhook_health: str | None
    imap_host: str | None
    imap_port: int | None

    model_config = {"from_attributes": True}


class ImapCheckNowResponse(BaseModel):
    fetched: int
    accepted: int
    error: str | None


class RecentImapMessageOut(BaseModel):
    """A raw fetched-message diagnostic entry, deliberately not run through
    (or gated by) the parser's own decision -- see app/domain/imap_polling.py's
    in-memory recent-message log for why this is never persisted to the
    database. decision/decision_reason are included precisely so this can
    answer "why wasn't this counted" without a second lookup.
    """

    uid: str
    fetched_at: datetime
    received_at: datetime
    sender: str
    subject: str
    body_snippet: str
    decision: str
    decision_reason: str | None


class ParserProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    template_version: str = Field(default="v1", max_length=50)
    sender_patterns: list[str] = Field(min_length=1)
    credit_keywords: list[str] | None = None
    reject_keywords: list[str] | None = None
    amount_pattern: str | None = None
    default_currency: str = Field(default="CAD", min_length=3, max_length=3)
    confidence_threshold: float = Field(default=0.75, ge=0, le=1)


class ParserProfileUpdateRequest(BaseModel):
    """All fields optional -- PATCH semantics, only supplied fields change."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    sender_patterns: list[str] | None = Field(default=None, min_length=1)
    credit_keywords: list[str] | None = None
    reject_keywords: list[str] | None = None
    amount_pattern: str | None = None
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    is_active: bool | None = None


class ParserProfileOut(BaseModel):
    id: str
    organization_id: str
    name: str
    template_version: str
    sender_patterns: list[str]
    credit_keywords: list[str]
    reject_keywords: list[str]
    amount_pattern: str
    default_currency: str
    confidence_threshold: float
    is_active: bool

    model_config = {"from_attributes": True}


class WebhookPayload(BaseModel):
    connection_id: str
    provider_message_id: str


class WebhookAck(BaseModel):
    status: str
    decision: ContributionDecision | None = None
    already_processed: bool = False
    event_id: str | None = None


class DisplayElementIn(BaseModel):
    id: str | None = None  # present on an existing element being kept/moved; absent on a new one
    type: ElementType
    x: float = 0
    y: float = 0
    width: float = Field(default=200, gt=0)
    height: float = Field(default=100, gt=0)
    z_index: int = 0
    style: dict[str, Any] = Field(default_factory=dict)
    binding: dict[str, Any] = Field(default_factory=dict)
    is_locked: bool = False
    is_hidden: bool = False


class DisplayElementOut(BaseModel):
    id: str
    type: ElementType
    x: float
    y: float
    width: float
    height: float
    z_index: int
    style: dict[str, Any]
    binding: dict[str, Any]
    is_locked: bool
    is_hidden: bool

    model_config = {"from_attributes": True}


class DisplayTemplateSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    canvas: dict[str, Any] | None = None
    is_default: bool = False
    elements: list[DisplayElementIn] = Field(default_factory=list)
    expected_version: int


class DisplayTemplateOut(BaseModel):
    id: str
    organization_id: str
    name: str
    canvas: dict[str, Any]
    is_default: bool
    version: int
    elements: list[DisplayElementOut]

    model_config = {"from_attributes": True}


class ReconciliationItemOut(BaseModel):
    id: str
    organization_id: str
    session_id: str
    contribution_event_id: str
    reason: str
    status: ReconciliationStatus
    resolution: ReconciliationResolution | None
    resolver_user_id: str | None
    resolved_at: datetime | None
    resolution_note: str | None
    corrected_amount: Decimal | None
    evidence: dict[str, Any]

    model_config = {"from_attributes": True}


class ReconciliationResolveRequest(BaseModel):
    resolution: ReconciliationResolution
    corrected_amount: Decimal | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)
    # Required only when resolution == REVERSED: the id of the earlier
    # ACCEPTED ContributionEvent this item's notification un-does.
    reverses_event_id: str | None = None


class PlanOut(BaseModel):
    id: str
    key: str
    name: str
    max_sessions_per_month: int
    max_mailbox_connections: int
    max_display_templates: int
    max_team_members: int
    allows_custom_subdomain: bool
    allows_sso: bool

    model_config = {"from_attributes": True}


class SessionReportOut(BaseModel):
    session_id: str
    status: SessionStatus
    validated_count: int
    validated_amount: Decimal | None
    excluded_counts: dict[str, int]
    corrections: list[ReconciliationItemOut]
    approval_history: list[dict[str, Any]]
    connection_health: dict[str, Any] | None


class AuditLogOut(BaseModel):
    id: str
    organization_id: str | None
    actor_user_id: str | None
    action: str
    target_type: str
    target_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    ip_address: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Support tickets (tenant-facing: app/api/v1/support.py) -----------------


class SupportTicketCreateRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class SupportTicketMessageCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class SupportTicketMessageOut(BaseModel):
    id: str
    ticket_id: str
    author_user_id: str
    author_is_admin: bool
    body: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SupportTicketOut(BaseModel):
    id: str
    organization_id: str
    created_by_user_id: str
    subject: str
    status: SupportTicketStatus
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SupportTicketDetailOut(SupportTicketOut):
    messages: list[SupportTicketMessageOut]


# --- Platform admin (app/api/v1/admin.py) ------------------------------------


class AdminOrganizationOut(BaseModel):
    id: str
    name: str
    plan_id: str | None
    plan_key: str | None
    plan_name: str | None
    subscription_status: SubscriptionStatus
    plan_starts_at: datetime | None
    plan_expires_at: datetime | None
    member_count: int
    created_at: datetime


class AdminOrganizationDetailOut(AdminOrganizationOut):
    grace_period_ends_at: datetime | None
    connections_total: int
    connections_connected: int


class AdminSubscriptionUpdateRequest(BaseModel):
    plan_id: str | None = None
    subscription_status: SubscriptionStatus | None = None
    # Record-keeping only, doesn't gate anything -- see Organization.plan_starts_at.
    plan_starts_at: datetime | None = None
    # When set, the background loop reverts plan_id back to Starter once this
    # passes (app/domain/subscriptions.py::revert_expired_plans). Passing
    # plan_id/subscription_status without this leaves the grant open-ended,
    # same as before this feature existed.
    plan_expires_at: datetime | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "AdminSubscriptionUpdateRequest":
        # field_validator wouldn't run at all if all fields are simply
        # omitted (left at their None defaults) -- this needs a model-level
        # check so an all-empty request (which would silently no-op) is
        # rejected outright instead.
        if (
            self.plan_id is None
            and self.subscription_status is None
            and self.plan_starts_at is None
            and self.plan_expires_at is None
        ):
            raise ValueError(
                "Provide at least one of plan_id, subscription_status, plan_starts_at, "
                "or plan_expires_at."
            )
        return self


class AdminSupportTicketOut(SupportTicketOut):
    organization_name: str
    admin_unread: bool


class AdminSupportTicketDetailOut(AdminSupportTicketOut):
    messages: list[SupportTicketMessageOut]


class SupportTicketStatusUpdateRequest(BaseModel):
    status: SupportTicketStatus
