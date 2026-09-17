from __future__ import annotations


MIN_PASSWORD_LENGTH = 12


class PasswordPolicyError(ValueError):
    """Raised without retaining or exposing the rejected password."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
