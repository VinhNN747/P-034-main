from collections.abc import Iterable

# This matrix is deliberately separate from Supabase Auth/RBAC. It documents
# which existing permission claim a domain operation may consume. It does not
# create, mint, or rename any Supabase permission.
DOMAIN_PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    "applications.read_own": frozenset({"applications.read_own"}),
    "applications.read_global": frozenset({"applications.read_global"}),
    "applications.create_own": frozenset({"applications.write_own", "applications.manage_own"}),
    "applications.update_own": frozenset({"applications.write_own", "applications.manage_own"}),
    "applications.delete_own": frozenset({"applications.manage_own"}),
    "models.available.read": frozenset({"models.read_available"}),
    "models.admin.read": frozenset({"models.read"}),
    "models.admin.manage": frozenset({"models.manage"}),
    "model_pools.read": frozenset({"model_pools.read_own"}),
    "model_pools.manage": frozenset({"model_pools.manage_own"}),
    "incidents.read": frozenset({"incidents.read"}),
    "incidents.manage": frozenset({"incidents.manage"}),
    "policy_reviews.read": frozenset({"routing_policies.read"}),
    "policy_reviews.decide": frozenset({"routing_policies.manage"}),
    "usage.read_own": frozenset({"usage.read_own"}),
    "usage.read_global": frozenset({"usage.read_global"}),
    "requests.read_own": frozenset({"requests.read_own"}),
    "requests.read_global": frozenset({"requests.read_global"}),
    "billing.read": frozenset({"billing.read"}),
    "billing.manage": frozenset({"billing.manage"}),
}


def has_domain_permission(permissions: Iterable[str], operation: str) -> bool:
    """Return whether existing claims authorize a named domain operation."""

    required_permissions = DOMAIN_PERMISSION_MATRIX.get(operation)
    if required_permissions is None:
        return False
    return bool(required_permissions.intersection(permissions))
