from fastapi import APIRouter

from app.api.v1 import (
    admin,
    audit,
    auth,
    connections,
    display_templates,
    join_requests,
    me,
    notifications,
    organizations,
    plans,
    reconciliation,
    reports,
    sessions,
    support,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(notifications.router)
api_router.include_router(organizations.router)
api_router.include_router(join_requests.router)
api_router.include_router(join_requests.me_router)
api_router.include_router(sessions.router)
api_router.include_router(sessions.org_sessions_router)
api_router.include_router(connections.router)
api_router.include_router(display_templates.router)
api_router.include_router(reconciliation.router)
api_router.include_router(reports.router)
api_router.include_router(plans.router)
api_router.include_router(audit.router)
api_router.include_router(support.router)
api_router.include_router(admin.router)
api_router.include_router(webhooks.router)
