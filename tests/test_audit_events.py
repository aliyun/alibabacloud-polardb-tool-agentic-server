from __future__ import annotations

from pathlib import Path


def test_dedicated_administration_mutations_have_audit_actions() -> None:
    api_root = Path(__file__).parents[1] / "server" / "api"
    source = "\n".join(
        (api_root / name).read_text()
        for name in (
            "dedicated_pools.py",
            "permission_templates.py",
            "db_instance_resources.py",
        )
    )

    required_fixed_actions = {
        "dedicated_pool.create",
        "dedicated_pool.update",
        "dedicated_pool.drain",
        "dedicated_pool.member.register",
        "permission_template.create",
        "permission_template.revision.create",
        "db_instance_resource.restore",
    }

    assert not {
        action for action in required_fixed_actions if action not in source
    }
    assert 'Literal["retry", "quarantine", "destroy"]' in source
    assert 'action=f"dedicated_pool.member.{action}"' in source
    assert 'action=f"permission_sync.{body.mode.value}"' in source
