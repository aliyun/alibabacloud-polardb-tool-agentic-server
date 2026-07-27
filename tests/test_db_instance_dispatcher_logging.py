import logging

from server.core.db_instance_dispatcher import _log_safe_failure


def test_whitelisted_vendor_code_logs_static_message(caplog):
    with caplog.at_level(logging.ERROR):
        _log_safe_failure(
            "dbi-1",
            "forward",
            "TENANT_CREATED",
            Exception(9900, "can't create tenant when enable_multi_tenant is OFF"),
        )
    record = caplog.records[-1]
    assert "dbi-1" in record.getMessage()
    assert "forward" in record.getMessage()
    assert "TENANT_CREATED" in record.getMessage()
    assert "code=9900" in record.getMessage()
    assert "enable_multi_tenant" in record.getMessage()


def test_non_whitelisted_code_omits_vendor_message(caplog):
    with caplog.at_level(logging.ERROR):
        _log_safe_failure(
            "dbi-1",
            "cleanup",
            "TENANT_DROPPED",
            Exception(1045, "Access denied for user 'sp'@'10.0.0.1'"),
        )
    message = caplog.records[-1].getMessage()
    assert "code=1045" in message
    assert "Access denied" not in message
    assert "sp" not in message.split("code=")[-1]


def test_non_driver_error_omits_message_and_code(caplog):
    with caplog.at_level(logging.ERROR):
        _log_safe_failure(
            "dbi-1",
            "cleanup",
            "RESIDUE_VERIFIED",
            TypeError("not enough arguments for format string"),
        )
    message = caplog.records[-1].getMessage()
    assert "TypeError" in message
    assert "code=None" in message
    assert "not enough arguments" not in message
