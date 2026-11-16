import pytest

from src.shared.dependencies.domain_permissions import (
    DOMAIN_PERMISSION_MATRIX,
    has_domain_permission,
)


@pytest.mark.parametrize(
    ("operation", "permissions"),
    [
        ("applications.read_own", {"applications.read_own"}),
        ("applications.create_own", {"applications.manage_own"}),
        ("applications.create_own", {"applications.write_own"}),
        ("models.available.read", {"models.read_available"}),
        ("models.admin.read", {"models.read"}),
        ("models.admin.manage", {"models.manage"}),
        ("incidents.read", {"incidents.read"}),
        ("incidents.manage", {"incidents.manage"}),
        ("policy_reviews.read", {"routing_policies.read"}),
        ("policy_reviews.decide", {"routing_policies.manage"}),
        ("usage.read_own", {"usage.read_own"}),
        ("usage.read_global", {"usage.read_global"}),
        ("billing.read", {"billing.read"}),
        ("billing.manage", {"billing.manage"}),
    ],
)
def test_existing_permission_grants_only_its_approved_operation(operation: str, permissions: set[str]) -> None:
    assert has_domain_permission(permissions, operation) is True


def test_compatibility_matrix_does_not_create_global_application_access() -> None:
    assert "applications.read_global" in DOMAIN_PERMISSION_MATRIX
    assert has_domain_permission({"usage.read_global"}, "applications.read_global") is False
    assert has_domain_permission({"models.manage"}, "applications.read_global") is False
    assert has_domain_permission({"applications.read_own"}, "applications.read_global") is False


def test_unknown_operation_is_denied() -> None:
    assert has_domain_permission({"billing.manage"}, "not-a-real-operation") is False
