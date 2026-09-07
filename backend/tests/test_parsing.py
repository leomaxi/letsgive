from datetime import datetime, timezone
from decimal import Decimal

from app.db.models.parser_profile import (
    DEFAULT_AMOUNT_PATTERN,
    DEFAULT_CREDIT_KEYWORDS,
    DEFAULT_REJECT_KEYWORDS,
    ParserProfile,
)
from app.domain.mailbox_providers import RawMessage
from app.domain.parsing import parse_message

MESSAGE = RawMessage(
    provider_message_id="msg-1",
    sender="deposits@fakebank.com",
    subject="Deposit received",
    body="You have received $42.50 CAD. Funds deposited to your account.",
    received_at=datetime.now(timezone.utc),
)


def _profile(sender_patterns: list[str], **overrides) -> ParserProfile:
    # ParserProfile's column defaults (credit/reject keywords, amount
    # pattern) are DB insert-time defaults, not applied when constructing
    # the object directly in Python without a flush -- so this helper
    # supplies them explicitly, same values the model would.
    return ParserProfile(
        organization_id="org-1",
        name="Test",
        sender_patterns=sender_patterns,
        credit_keywords=list(DEFAULT_CREDIT_KEYWORDS),
        reject_keywords=list(DEFAULT_REJECT_KEYWORDS),
        amount_pattern=DEFAULT_AMOUNT_PATTERN,
        default_currency="CAD",
        confidence_threshold=0.75,
        **overrides,
    )


def test_no_match_has_zero_specificity():
    result = parse_message(MESSAGE, _profile(["other@elsewhere.com"]))
    assert result.matched_source is False
    assert result.match_specificity == 0


def test_domain_wildcard_match_has_lower_specificity_than_exact_address():
    domain_result = parse_message(MESSAGE, _profile(["@fakebank.com"]))
    exact_result = parse_message(MESSAGE, _profile(["deposits@fakebank.com"]))

    assert domain_result.matched_source is True
    assert exact_result.matched_source is True
    assert domain_result.match_specificity < exact_result.match_specificity


def test_specificity_is_populated_on_every_matched_return_path():
    # Reject-keyword short-circuit.
    reject_msg = RawMessage(**{**MESSAGE.__dict__, "body": "This is a cancellation request."})
    reject_result = parse_message(reject_msg, _profile(["deposits@fakebank.com"]))
    assert reject_result.matched_source is True
    assert reject_result.match_specificity > 0

    # No-credit-intent short-circuit.
    neutral_msg = RawMessage(**{**MESSAGE.__dict__, "body": "Your statement is ready to view."})
    neutral_result = parse_message(neutral_msg, _profile(["deposits@fakebank.com"]))
    assert neutral_result.matched_source is True
    assert neutral_result.match_specificity > 0

    # Full successful parse.
    full_result = parse_message(MESSAGE, _profile(["deposits@fakebank.com"]))
    assert full_result.matched_source is True
    assert full_result.match_specificity > 0


def test_a_profile_with_multiple_patterns_takes_the_most_specific_one_that_matches():
    # This sender matches both an exact pattern and a domain wildcard within
    # the *same* profile -- the exact one should win, not whichever was
    # listed first.
    result = parse_message(MESSAGE, _profile(["@fakebank.com", "deposits@fakebank.com"]))
    assert result.match_specificity == 2


def test_a_real_interac_deposit_is_not_rejected_by_its_own_boilerplate():
    # Real production bug: every Interac transactional email -- deposit
    # notifications included -- carries this exact disclaimer sentence in
    # its footer, verbatim, regardless of what the email is actually about.
    # DEFAULT_REJECT_KEYWORDS used to include "request", which matched this
    # boilerplate and rejected every single real Interac deposit outright,
    # unconditionally, no matter what the rest of the message said. Body
    # text below is taken directly from a real deposit notification.
    message = RawMessage(
        provider_message_id="msg-interac-real",
        sender="notify@payments.interac.ca",
        subject=(
            "Interac e-Transfer: You've received $1.00 from LEONARD MAXIMUS MENSAH "
            "and it has been automatically deposited."
        ),
        body=(
            "Funds Deposited! $1.00 Your funds have been automatically deposited into "
            "your account at Scotiabank. Amount: $1.00 (CAD) For your security, please "
            "do not forward this email as it contains confidential information meant "
            "only for you. Interac will never request access to this email notification "
            "from you. Click here to manage notification preferences from this contact."
        ),
        received_at=datetime.now(timezone.utc),
    )
    result = parse_message(message, _profile(["notify@payments.interac.ca"]))
    assert result.has_credit_intent is True
    assert result.amount == Decimal("1.00")
    assert result.reason is None
