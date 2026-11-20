import pytest
from pydantic import ValidationError

from src.backend.routers.account_recovery import SignupRequest


@pytest.mark.parametrize("password", [
    "a" * 7,
    "a" * 8,
    "a" * 11,
    "short-pass",
])
def test_signup_rejects_passwords_shorter_than_twelve_characters(password):
    with pytest.raises(ValidationError):
        SignupRequest(email="user@example.com", password=password)


@pytest.mark.parametrize("password", [
    "a" * 12,
    "a" * 13,
    "a" * 128,
    "exactly-twelve-1",
])
def test_signup_accepts_passwords_with_twelve_or_more_characters(password):
    payload = SignupRequest(email="user@example.com", password=password)
    assert len(payload.password) >= 12
