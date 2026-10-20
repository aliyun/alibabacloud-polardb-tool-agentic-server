import pytest

from server.auth.password_policy import PasswordPolicyError, validate_password


@pytest.mark.parametrize("length", [0, 7, 8, 9, 11, 12])
def test_password_minimum_eight(length):
    password = "x" * length
    if length < 8:
        with pytest.raises(PasswordPolicyError, match="at least 8 characters") as error:
            validate_password(password)
        if password:
            assert password not in str(error.value)
    else:
        validate_password(password)
