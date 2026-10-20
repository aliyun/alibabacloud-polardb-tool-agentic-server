import base64

import pytest

from server.core.crypto import encrypt
from server.core.identity_source_space import identity_source_binds_all_spaces
from server.models import EnterpriseIdentitySource


@pytest.fixture(autouse=True)
def root_key(monkeypatch):
    monkeypatch.setenv('PAS_ENCRYPTION_KEY', base64.b64encode(b't' * 32).decode())


@pytest.mark.parametrize('raw', ['not-json', 'null', '[]', '"text"', '{"bind_all_spaces": null}', '{"bind_all_spaces": 1}'])
def test_invalid_config_does_not_enable_automatic_binding(raw):
    source = EnterpriseIdentitySource(config_ciphertext=encrypt(raw))
    assert identity_source_binds_all_spaces(source) is False


def test_wrong_key_does_not_enable_automatic_binding():
    source = EnterpriseIdentitySource(config_ciphertext=encrypt('{}', key=b'x' * 32))
    assert identity_source_binds_all_spaces(source) is False


@pytest.mark.parametrize('raw, expected', [('{}', True), ('{"bind_all_spaces": true}', True), ('{"bind_all_spaces": false}', False)])
def test_valid_config_preserves_binding_policy(raw, expected):
    source = EnterpriseIdentitySource(config_ciphertext=encrypt(raw))
    assert identity_source_binds_all_spaces(source) is expected


def test_missing_legacy_config_preserves_default():
    assert identity_source_binds_all_spaces(EnterpriseIdentitySource(config_ciphertext=None)) is True
