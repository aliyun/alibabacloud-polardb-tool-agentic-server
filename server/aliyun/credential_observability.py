from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from server.aliyun.diagnostics import safe_error_code


logger = logging.getLogger(__name__)

CredentialMetricName = Literal[
    "aliyun_credential_refresh",
    "aliyun_credential_provider_rebuild",
    "aliyun_credential_request_failure",
]
MetricSink = Callable[["CredentialMetricSample"], None]
_metric_sink: MetricSink | None = None
_MODES = frozenset({"direct_ak", "assume_role", "ecs_ram_role"})
_ERROR_CODES = frozenset(
    {
        "OPENAPI_CREDENTIAL_INVALID",
        "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID",
        "OPENAPI_STS_ASSUME_ROLE_DENIED",
        "OPENAPI_STS_ROLE_TRUST_REJECTED",
        "OPENAPI_STS_EXTERNAL_ID_MISMATCH",
        "OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED",
        "OPENAPI_ECS_METADATA_DISABLED",
        "OPENAPI_ECS_IMDSV2_UNAVAILABLE",
        "OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED",
        "OPENAPI_PERMISSION_DENIED",
        "OPENAPI_DNS_FAILURE",
        "OPENAPI_TLS_FAILURE",
        "OPENAPI_CONNECT_FAILURE",
        "OPENAPI_ENDPOINT_UNSUPPORTED",
    }
)


@dataclass(frozen=True, slots=True)
class CredentialMetricSample:
    name: str
    fields: dict[str, str | float]


def set_credential_metric_sink(sink: MetricSink | None) -> None:
    """Register an in-process integration hook for sanitized samples."""
    global _metric_sink
    _metric_sink = sink


def _safe_mode(value: object) -> str:
    return value if isinstance(value, str) and value in _MODES else "unknown"


def _safe_number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)) and math.isfinite(value):
        return max(0.0, float(value))
    return 0.0


def _safe_error_code(value: object) -> str:
    code = safe_error_code(value)
    return code if code in _ERROR_CODES else "UNKNOWN"


def _sanitized_sample(
    sample: CredentialMetricSample,
) -> CredentialMetricSample | None:
    if sample.name not in {
        "aliyun_credential_refresh",
        "aliyun_credential_provider_rebuild",
        "aliyun_credential_request_failure",
    }:
        return None
    fields = sample.fields
    safe: dict[str, str | float] = {"mode": _safe_mode(fields.get("mode"))}
    if sample.name == "aliyun_credential_refresh":
        safe["outcome"] = (
            fields.get("outcome")
            if fields.get("outcome") in {"success", "failure"}
            else "failure"
        )
        safe["duration_seconds"] = _safe_number(
            fields.get("duration_seconds")
        )
        if "expires_in_seconds" in fields:
            safe["expires_in_seconds"] = _safe_number(
                fields.get("expires_in_seconds")
            )
        if safe["outcome"] == "failure":
            safe["error_code"] = _safe_error_code(fields.get("error_code"))
    elif sample.name == "aliyun_credential_provider_rebuild":
        safe["outcome"] = (
            fields.get("outcome")
            if fields.get("outcome") in {"success", "failure"}
            else "failure"
        )
    else:
        safe["error_code"] = _safe_error_code(fields.get("error_code"))
    return CredentialMetricSample(name=sample.name, fields=safe)


def emit_credential_metric(sample: CredentialMetricSample) -> None:
    """Log and optionally forward a sample without exposing caller objects."""
    safe_sample = _sanitized_sample(sample)
    if safe_sample is None:
        return
    logger.info(
        "aliyun credential metric",
        extra={"metric": safe_sample.name, **safe_sample.fields},
    )
    if _metric_sink is not None:
        try:
            _metric_sink(safe_sample)
        except Exception:
            # Do not pass a sink exception to product logs, either.
            logger.warning("aliyun credential metric sink failed")
