from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.db.models.contribution_event import ContributionDecision
from app.db.models.display_template import ElementType
from app.db.models.mailbox_connection import ConnectionStatus, MailboxProviderName
from app.db.models.membership import MembershipStatus, Role
from app.db.models.organization import SubscriptionStatus
from app.db.models.reconciliation_item import ReconciliationResolution, ReconciliationStatus
from app.db.models.session import SessionStatus


class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10)
    full_name: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    mfa_enabled: bool

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


class ParserProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    template_version: str = Field(default="v1", max_length=50)
    sender_patterns: list[str] = Field(min_length=1)
    credit_keywords: list[str] | None = None
    reject_keywords: list[str] | None = None
    amount_pattern: str | None = None
    default_currency: str = Field(default="CAD", min_length=3, max_length=3)
    confidence_threshold: float = Field(default=0.75, ge=0, le=1)


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
