from __future__ import annotations

import calendar
import json
import time
from collections.abc import Mapping
from typing import Any

from Tea.core import TeaCore
from alibabacloud_credentials.exceptions import CredentialException
from alibabacloud_credentials.provider.ram_role_arn import (
    RamRoleArnCredentialsProvider,
    _get_stale_time,
)
from alibabacloud_credentials.provider.refreshable import (
    Credentials,
    RefreshResult,
)
from alibabacloud_credentials.utils import parameter_helper as ph

from server.aliyun.diagnostics import safe_error_code
from server.logging import safe_request_id


class SafeAssumeRoleFailure(CredentialException):
    """Body-free non-success response from the official RAM-role provider."""

    def __init__(self, code: str | None, request_id: str | None) -> None:
        self.code = safe_error_code(code)
        self.request_id = safe_request_id(request_id)
        self.message = "AssumeRole credential request failed"
        Exception.__init__(self, self.message)


class SafeRamRoleArnCredentialsProvider(RamRoleArnCredentialsProvider):
    """Official provider behavior with a body-free STS failure boundary."""

    def _new_assume_role_request(self, pre_credentials: Credentials):
        tea_request = ph.get_new_request()
        tea_request.query = {
            "Action": "AssumeRole",
            "Format": "JSON",
            "Version": "2015-04-01",
            "DurationSeconds": str(self._duration_seconds),
            "RoleArn": self._role_arn,
            "RoleSessionName": self._role_session_name,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "Timestamp": ph.get_iso_8061_date(),
            "SignatureNonce": ph.get_uuid(),
        }
        if self._policy:
            tea_request.query["Policy"] = self._policy
        if self._external_id:
            tea_request.query["ExternalId"] = self._external_id
        tea_request.query["AccessKeyId"] = pre_credentials.get_access_key_id()
        security_token = pre_credentials.get_security_token()
        if security_token:
            tea_request.query["SecurityToken"] = security_token
        string_to_sign = ph.compose_string_to_sign("GET", tea_request.query)
        signature = ph.sign_string(
            string_to_sign, pre_credentials.get_access_key_secret() + "&"
        )
        tea_request.query["Signature"] = signature
        tea_request.protocol = "https"
        tea_request.headers["host"] = self._sts_endpoint
        return tea_request

    @staticmethod
    def _decode_response(response: Any) -> Mapping[str, Any] | None:
        body = getattr(response, "body", b"")
        try:
            decoded = json.loads(bytes(body).decode("utf-8"))
            return decoded if isinstance(decoded, Mapping) else None
        except Exception:
            return None
        finally:
            del body

    @classmethod
    def _failure_from_response(cls, response: Any) -> SafeAssumeRoleFailure:
        payload = cls._decode_response(response)
        try:
            code = payload.get("Code") if payload is not None else None
            request_id = (
                payload.get("RequestId") if payload is not None else None
            )
            return SafeAssumeRoleFailure(code, request_id)
        finally:
            del payload

    def _credentials_from_response(
        self, response: Any, pre_credentials: Credentials
    ) -> RefreshResult[Credentials]:
        if response.status_code != 200:
            raise self._failure_from_response(response)
        payload = self._decode_response(response)
        try:
            credentials = payload.get("Credentials") if payload else None
            if not isinstance(credentials, Mapping):
                raise SafeAssumeRoleFailure(None, None)
            required = ("AccessKeyId", "AccessKeySecret", "SecurityToken")
            if not all(credentials.get(field) for field in required):
                raise SafeAssumeRoleFailure(None, None)
            expiration = calendar.timegm(
                time.strptime(
                    str(credentials.get("Expiration")),
                    "%Y-%m-%dT%H:%M:%SZ",
                )
            )
            value = Credentials(
                access_key_id=credentials.get("AccessKeyId"),
                access_key_secret=credentials.get("AccessKeySecret"),
                security_token=credentials.get("SecurityToken"),
                expiration=expiration,
                provider_name=(
                    f"{self.get_provider_name()}/"
                    f"{pre_credentials.get_provider_name()}"
                ),
            )
            return RefreshResult(
                value=value, stale_time=_get_stale_time(expiration)
            )
        except SafeAssumeRoleFailure:
            raise
        except Exception:
            raise SafeAssumeRoleFailure(None, None) from None
        finally:
            del payload

    def _refresh_credentials(self) -> RefreshResult[Credentials]:
        pre_credentials = self._credentials_provider.get_credentials()
        if pre_credentials is None:
            raise SafeAssumeRoleFailure(None, None)
        response = TeaCore.do_action(
            self._new_assume_role_request(pre_credentials), self._runtime_options
        )
        return self._credentials_from_response(response, pre_credentials)

    async def _refresh_credentials_async(self) -> RefreshResult[Credentials]:
        pre_credentials = await self._credentials_provider.get_credentials_async()
        if pre_credentials is None:
            raise SafeAssumeRoleFailure(None, None)
        response = await TeaCore.async_do_action(
            self._new_assume_role_request(pre_credentials), self._runtime_options
        )
        return self._credentials_from_response(response, pre_credentials)
