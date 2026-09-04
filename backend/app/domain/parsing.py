import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.db.models.parser_profile import ParserProfile
from app.domain.mailbox_providers import RawMessage


@dataclass
class ParseResult:
    matched_source: bool
    has_credit_intent: bool
    amount: Decimal | None
    currency: str | None
    confidence: float
    reason: str | None = None
    # How specifically the sender matched this profile's patterns: 0 (no
    # match), 1 (a "@domain.com" wildcard), or 2 (an exact address). Lets an
    # org with genuinely overlapping profiles -- one for "@bank.com" broadly,
    # another for the specific "deposits@bank.com" -- get deterministic,
    # sensible tie-breaking (the more specific profile wins) instead of
    # whichever profile the database happened to return first.
    match_specificity: int = 0


_DOMAIN_MATCH = 1
_EXACT_MATCH = 2


def _sender_match_specificity(sender: str, patterns: list[str]) -> int:
    sender_lower = sender.lower()
    best = 0
    for pattern in patterns:
        pattern_lower = pattern.lower()
        if pattern_lower.startswith("@"):
            if sender_lower.endswith(pattern_lower):
                best = max(best, _DOMAIN_MATCH)
        elif sender_lower == pattern_lower:
            best = max(best, _EXACT_MATCH)
    return best


def _has_any_keyword(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)


def parse_message(message: RawMessage, profile: ParserProfile) -> ParseResult:
    """Runs one message through one tenant parser profile (spec 5.3).

    Confidence is intentionally simple and explainable (not ML-based): it
    starts at 1.0 and is discounted for each signal that isn't a clean, sure
    match, so a human reviewing a reconciliation queue entry can see exactly
    why a message landed below the profile's threshold.
    """
    specificity = _sender_match_specificity(message.sender, profile.sender_patterns)
    if specificity == 0:
        return ParseResult(
            matched_source=False,
            has_credit_intent=False,
            amount=None,
            currency=None,
            confidence=0.0,
            reason=f"Sender '{message.sender}' does not match any configured pattern.",
        )

    full_text = f"{message.subject}\n{message.body}"

    if _has_any_keyword(full_text, profile.reject_keywords):
        return ParseResult(
            matched_source=True,
            has_credit_intent=False,
            amount=None,
            currency=None,
            confidence=0.0,
            reason="Message contains reject language (request/cancellation/reminder/etc).",
            match_specificity=specificity,
        )

    has_credit_intent = _has_any_keyword(full_text, profile.credit_keywords)
    if not has_credit_intent:
        return ParseResult(
            matched_source=True,
            has_credit_intent=False,
            amount=None,
            currency=None,
            confidence=0.0,
            reason="No unambiguous credit-intent language found.",
            match_specificity=specificity,
        )

    confidence = 1.0
    match = re.search(profile.amount_pattern, full_text, re.IGNORECASE)
    amount: Decimal | None = None
    currency: str | None = None
    if match is None:
        confidence -= 0.5
    else:
        currency = (match.groupdict().get("currency") or profile.default_currency).upper()
        try:
            amount = Decimal(match.group("amount").replace(",", ""))
        except (InvalidOperation, IndexError):
            confidence -= 0.5

    return ParseResult(
        matched_source=True,
        has_credit_intent=True,
        amount=amount,
        currency=currency,
        confidence=max(confidence, 0.0),
        reason=None if match else "Could not extract a confident amount from the message.",
        match_specificity=specificity,
    )
