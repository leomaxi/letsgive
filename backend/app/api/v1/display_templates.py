from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import DisplayTemplateOut, DisplayTemplateSaveRequest
from app.db.base import new_uuid
from app.db.models.display_element import DisplayElement
from app.db.models.display_template import DEFAULT_CANVAS, DisplayTemplate
from app.db.models.membership import Membership, Role
from app.db.models.organization import Organization
from app.db.models.session import Session
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.billing import assert_can_create_display_template
from app.domain.rbac import require_roles

router = APIRouter(prefix="/v1/organizations/{organization_id}/display-templates", tags=["display"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _get_owned_template(
    db: AsyncSession, organization_id: str, template_id: str
) -> DisplayTemplate:
    result = await db.execute(
        select(DisplayTemplate)
        .options(selectinload(DisplayTemplate.elements))
        .where(DisplayTemplate.id == template_id, DisplayTemplate.organization_id == organization_id)
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    return template


async def _clear_other_defaults(db: AsyncSession, organization_id: str, keep_id: str | None) -> None:
    result = await db.execute(
        select(DisplayTemplate).where(
            DisplayTemplate.organization_id == organization_id,
            DisplayTemplate.is_default.is_(True),
            DisplayTemplate.id != keep_id,
        )
    )
    for other in result.scalars().all():
        other.is_default = False


@router.post("", response_model=DisplayTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_display_template(
    organization_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DisplayTemplate:
    require_roles(membership, Role.OWNER, Role.MEDIA)

    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    await assert_can_create_display_template(db, org)

    template = DisplayTemplate(
        organization_id=organization_id,
        created_by_user_id=current_user.id,
        name="Untitled template",
        canvas=dict(DEFAULT_CANVAS),
    )
    db.add(template)
    await db.flush()

    await record_audit_event(
        db,
        action="display_template.created",
        target_type="display_template",
        target_id=template.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        ip_address=_client_ip(request),
    )

    await db.commit()
    return await _get_owned_template(db, organization_id, template.id)


@router.get("", response_model=list[DisplayTemplateOut])
async def list_display_templates(
    organization_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[DisplayTemplate]:
    result = await db.execute(
        select(DisplayTemplate)
        .options(selectinload(DisplayTemplate.elements))
        .where(DisplayTemplate.organization_id == organization_id)
    )
    return list(result.scalars().all())


@router.get("/{template_id}", response_model=DisplayTemplateOut)
async def get_display_template(
    organization_id: str,
    template_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> DisplayTemplate:
    return await _get_owned_template(db, organization_id, template_id)


@router.put("/{template_id}", response_model=DisplayTemplateOut)
async def save_display_template(
    organization_id: str,
    template_id: str,
    payload: DisplayTemplateSaveRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DisplayTemplate:
    require_roles(membership, Role.OWNER, Role.MEDIA)

    template = await _get_owned_template(db, organization_id, template_id)
    if template.version != payload.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Template was saved by someone else (expected version "
                f"{payload.expected_version}, current version {template.version})."
            ),
        )

    template.name = payload.name
    if payload.canvas is not None:
        template.canvas = payload.canvas
    template.version += 1

    if payload.is_default:
        await _clear_other_defaults(db, organization_id, keep_id=template.id)
    template.is_default = payload.is_default

    # Full-document replace: autosave sends the whole canvas each time, so
    # the simplest correct approach is to drop the old element set and
    # recreate it -- element identity isn't meaningful across saves here
    # since there's no frontend yet relying on stable ids for e.g. animation.
    for element in list(template.elements):
        await db.delete(element)
    await db.flush()

    template.elements = [
        DisplayElement(
            template_id=template.id,
            type=element_in.type,
            x=element_in.x,
            y=element_in.y,
            width=element_in.width,
            height=element_in.height,
            z_index=element_in.z_index,
            style=element_in.style,
            binding=element_in.binding,
            is_locked=element_in.is_locked,
            is_hidden=element_in.is_hidden,
        )
        for element_in in payload.elements
    ]

    await record_audit_event(
        db,
        action="display_template.saved",
        target_type="display_template",
        target_id=template.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        after={"name": template.name, "element_count": len(template.elements)},
        ip_address=_client_ip(request),
    )

    await db.commit()
    return await _get_owned_template(db, organization_id, template.id)


@router.post(
    "/{template_id}/duplicate",
    response_model=DisplayTemplateOut,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_display_template(
    organization_id: str,
    template_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DisplayTemplate:
    require_roles(membership, Role.OWNER, Role.MEDIA)

    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    await assert_can_create_display_template(db, org)

    original = await _get_owned_template(db, organization_id, template_id)

    copy = DisplayTemplate(
        id=new_uuid(),  # set explicitly so it's known below without an intervening flush
        organization_id=organization_id,
        created_by_user_id=current_user.id,
        name=f"{original.name} (copy)",
        canvas=dict(original.canvas),
        is_default=False,
    )
    db.add(copy)
    # Assign .elements while `copy` is still pending (pre-flush): once it's
    # persistent, touching this relationship attribute makes SQLAlchemy
    # lazy-load its "current" value to diff against, which needs a real
    # query and can't run synchronously in this async context.
    copy.elements = [
        DisplayElement(
            type=el.type,
            x=el.x,
            y=el.y,
            width=el.width,
            height=el.height,
            z_index=el.z_index,
            style=dict(el.style),
            binding=dict(el.binding),
            is_locked=el.is_locked,
            is_hidden=el.is_hidden,
        )
        for el in original.elements
    ]

    await record_audit_event(
        db,
        action="display_template.duplicated",
        target_type="display_template",
        target_id=copy.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        after={"source_template_id": original.id},
        ip_address=_client_ip(request),
    )

    await db.commit()
    return await _get_owned_template(db, organization_id, copy.id)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_display_template(
    organization_id: str,
    template_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    require_roles(membership, Role.OWNER, Role.MEDIA)

    template = await _get_owned_template(db, organization_id, template_id)

    # Deliberately app-level rather than relying on an ON DELETE SET NULL FK:
    # SQLite (the dev default) doesn't enforce FK constraints unless a
    # connection explicitly turns them on, so a DB-only cascade would work
    # on Postgres and silently leave dangling references in dev/tests.
    result = await db.execute(select(Session).where(Session.display_template_id == template.id))
    for session in result.scalars().all():
        session.display_template_id = None

    await db.delete(template)

    await record_audit_event(
        db,
        action="display_template.deleted",
        target_type="display_template",
        target_id=template_id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        ip_address=_client_ip(request),
    )

    await db.commit()
