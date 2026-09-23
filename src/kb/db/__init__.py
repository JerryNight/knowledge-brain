"""Database access layer.

``kb.db.engine``  — engines and session factories (app role vs admin role)
``kb.db.tenant``  — per-transaction ``app.user_id`` binding for RLS
"""

from kb.db.engine import (
    dispose_engines,
    get_admin_engine,
    get_admin_sessionmaker,
    get_app_engine,
    get_app_sessionmaker,
)
from kb.db.tenant import (
    MissingTenantContext,
    as_tenant_id,
    current_tenant,
    set_tenant,
    tenant_transaction,
)

__all__ = [
    "MissingTenantContext",
    "as_tenant_id",
    "current_tenant",
    "dispose_engines",
    "get_admin_engine",
    "get_admin_sessionmaker",
    "get_app_engine",
    "get_app_sessionmaker",
    "set_tenant",
    "tenant_transaction",
]
