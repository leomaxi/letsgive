from datetime import datetime, timezone

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
