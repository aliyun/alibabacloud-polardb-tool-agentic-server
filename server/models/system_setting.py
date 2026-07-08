from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


class SystemSetting(TimestampMixin, Base):
    __tablename__ = "system_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


@dataclass
class SettingDef:
    type: str  # "int", "string", "bool", "secret"
    default: str
    required: bool
    description: str


SETTINGS_SCHEMA: dict[str, SettingDef] = {
    "pool_target_size":         SettingDef(type="int",    default="0",    required=False, description="Hot pool target (0=disabled)"),
    "pool_region_id":           SettingDef(type="string", default="",     required=True,  description="Region"),
    "pool_vpc_id":              SettingDef(type="string", default="",     required=True,  description="VPC ID"),
    "pool_vswitch_id":          SettingDef(type="string", default="",     required=True,  description="VSwitch ID"),
    "pool_zone_id":             SettingDef(type="string", default="",     required=True,  description="Availability zone"),
    "pool_security_ip_list":    SettingDef(type="string", default="127.0.0.1", required=True, description="IP whitelist"),
    "pool_db_type":             SettingDef(type="string", default="mysql",              required=False, description="Engine type"),
    "pool_db_version":          SettingDef(type="string", default="8.0",               required=False, description="Engine version"),
    "pool_db_minor_version":    SettingDef(type="string", default="8.0.2",                  required=False, description="Engine minor version"),
    "pool_db_node_class":       SettingDef(type="string", default="polar.mysql.sl.small.c", required=False, description="Node class"),
    "pool_proxy_class":         SettingDef(type="string", default="polar.maxscale.g2.medium.c", required=False, description="Proxy class"),
    "pool_proxy_type":          SettingDef(type="string", default="GENERAL",           required=False, description="Proxy type"),
    "pool_architecture":        SettingDef(type="string", default="X86",               required=False, description="Architecture"),
    "pool_loose_polar_log_bin": SettingDef(type="string", default="OFF",               required=False, description="Polar log bin"),
    "pool_loose_x_engine":      SettingDef(type="string", default="OFF",               required=False, description="X-Engine"),
    "pool_pay_type":            SettingDef(type="string", default="Postpaid",          required=False, description="Billing"),
    "pool_serverless_type":     SettingDef(type="string", default="AgileServerless",   required=False, description="Serverless type"),
    "pool_scale_min":           SettingDef(type="int",    default="1",                 required=False, description="Min PCU"),
    "pool_scale_max":           SettingDef(type="int",    default="4",                 required=False, description="Max PCU"),
    "pool_allow_shut_down":     SettingDef(type="bool",   default="true",              required=False, description="Scale to zero"),
    "pool_scale_ro_num_min":    SettingDef(type="int",    default="0",                 required=False, description="Min RO nodes"),
    "pool_scale_ro_num_max":    SettingDef(type="int",    default="1",                 required=False, description="Max RO nodes"),
    "pool_storage_type":        SettingDef(type="string", default="essdpl0",           required=False, description="Storage type"),
    "pool_storage_space":       SettingDef(type="int",    default="20",                required=False, description="Storage space (GB)"),
    "pool_endpoint_net_type":   SettingDef(type="string", default="Private",           required=False, description="Endpoint network type (Private or Public)"),
    "provisioning_poll_timeout_seconds": SettingDef(type="int", default="600", required=False, description="Max seconds to poll cluster status"),
    "retry_after_seconds":      SettingDef(type="int",    default="10",   required=False, description="Seconds client should wait before retrying CREATING instance"),
    "aliyun_credential_mode":      SettingDef(type="string", default="direct_ak", required=True,  description="Credential mode (direct_ak or assume_role)"),
    "aliyun_access_key_id":        SettingDef(type="secret", default="",          required=True,  description="Access Key ID"),
    "aliyun_access_key_secret":    SettingDef(type="secret", default="",          required=True,  description="Access Key Secret"),
    "aliyun_role_arn":              SettingDef(type="string", default="",          required=False, description="RAM Role ARN (for assume_role mode)"),
    "aliyun_role_session_name":    SettingDef(type="string", default="polardb-agentic", required=False, description="Role session name"),
    "aliyun_sts_duration_seconds": SettingDef(type="int",    default="3600",      required=False, description="STS token duration (seconds)"),
}
