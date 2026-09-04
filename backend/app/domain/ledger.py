from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.contribution_event import ContributionDecision, ContributionEvent


async def compute_ledger_totals(db: AsyncSession, session_id: str) -> tuple[int, Decimal | None]:
    """A session's public/operator/report contribution count and total --
    always derived by aggregating the ledger, never a separately-mutated
    counter (spec 10.1), so there is nothing to race or double-increment.
    The one shared implementation both sessions.py (public/operator totals)
    and reports.py (session reports) call, so the two never drift apart.

    ACCEPTED events count in full. REVERSED events (spec 7's "reversed"
    review category -- a later notification un-does an earlier accepted
    deposit) net *out* of both the count and the total: a reversal isn't
    itself a new contribution, it's the earlier one un-happening, so it
    should make the numbers go back down, not up. REVERSED rows always
    carry a positive `amount` (the magnitude being reversed, via
    `corrects_event_id` pointing at the original ACCEPTED event) -- the
    subtraction happens here, not by storing a signed amount on the row.
    """
    result = await db.execute(
        select(func.count(ContributionEvent.id), func.sum(ContributionEvent.amount)).where(
            ContributionEvent.session_id == session_id,
            ContributionEvent.decision == ContributionDecision.ACCEPTED,
        )
    )
    accepted_count, accepted_sum = result.one()

    result = await db.execute(
        select(func.count(ContributionEvent.id), func.sum(ContributionEvent.amount)).where(
            ContributionEvent.session_id == session_id,
            ContributionEvent.decision == ContributionDecision.REVERSED,
        )
    )
    reversed_count, reversed_sum = result.one()

    count = (accepted_count or 0) - (reversed_count or 0)
    total = None
    if accepted_sum is not None or reversed_sum is not None:
        total = (accepted_sum or Decimal("0")) - (reversed_sum or Decimal("0"))
    return count, total
