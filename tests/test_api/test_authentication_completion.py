from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from src.shared.core import security


@pytest.mark.parametrize("method, anonymous, complete", [
    ("password", False, True), ("recovery", False, False), ("password", True, False),
])
def test_verified_jwt_distinguishes_recovery_from_completed_authentication(monkeypatch, method, anonymous, complete):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    token = jwt.encode({
        "sub": "user-a", "email": "user-a@example.com", "aud": "authenticated",
        "iss": "https://auth.example.com/auth/v1", "iat": now, "exp": now + timedelta(minutes=15),
        "amr": [{"method": method, "timestamp": int(now.timestamp())}], "is_anonymous": anonymous,
    }, key, algorithm="ES256")
    monkeypatch.setattr(security, "get_signing_key", lambda _: key.public_key())
    monkeypatch.setattr(security, "get_settings", lambda: SimpleNamespace(
        supabase_url="https://auth.example.com", supabase_jwt_audience="authenticated",
    ))
    claims = security.verify_access_token(token)
    assert claims.user_id == "user-a"
    assert claims.authentication_complete is complete
