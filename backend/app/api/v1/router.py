from fastapi import APIRouter

from app.api.v1 import (
    audit,
    auth,
    connections,
    display_templates,
    me,
    organizations,
    plans,
    reconciliation,
    reports,
    sessions,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(organizations.router)
api_router.include_router(sessions.router)
api_router.include_router(sessions.org_sessions_router)
api_router.include_router(connections.router)
api_router.include_router(display_templates.router)
api_router.include_router(reconciliation.router)
api_router.include_router(reports.router)
api_router.include_router(plans.router)
api_router.include_router(audit.router)
api_router.include_router(webhooks.router)
