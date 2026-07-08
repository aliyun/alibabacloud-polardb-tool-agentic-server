"""Shared test helpers."""

import server.auth.jwt_manager as _jm
from server.auth.jwt_manager import _generate_rsa_key_pair


def init_test_jwt_keys() -> None:
    """Initialize JWT keys for tests after reset_keys()/reset_config().

    Generates a fresh RSA key pair and sets it on the jwt_manager module
    globals only.  _load_keys() checks globals first (already-loaded), so
    this is sufficient for all test calls to create_access_token/verify_token.

    Does NOT call get_config() or modify the config object, because doing so
    would cache a stale config (e.g. including PAS_ENCRYPTION_KEY from the
    shell env) that could break fixtures that override env vars later.
    """
    priv_pem, pub_pem = _generate_rsa_key_pair()
    _jm._private_key = priv_pem
    _jm._public_key = pub_pem
