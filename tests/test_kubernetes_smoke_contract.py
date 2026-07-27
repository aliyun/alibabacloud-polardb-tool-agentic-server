from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "scripts" / "deploy" / "smoke-helm.sh"


def test_smoke_script_accepts_restricted_network_kind_image() -> None:
    script = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert "--kind-image)" in script
    assert 'KIND_IMAGE="$2"' in script
    assert 'kind create cluster "${kind_create_args[@]}"' in script
    assert 'kind_create_args+=(--image "$KIND_IMAGE")' in script
