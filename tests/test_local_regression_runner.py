import pytest

from scripts.dev.run_local_regression import clean_environment, owned_database_name, integration_environment


@pytest.mark.parametrize("name", ["pas", "mysql", "postgres", "pas_local_reg_public", "pas_local_reg_" + "a" * 32 + ";DROP DATABASE pas"])
def test_local_runner_refuses_to_drop_unowned_database_names(name):
    with pytest.raises(ValueError, match="owned"):
        owned_database_name(name)


def test_local_runner_accepts_only_its_random_database_namespace():
    name = "pas_local_reg_" + "a" * 32
    assert owned_database_name(name) == name


def test_local_runner_does_not_inherit_database_cloud_or_proxy_settings():
    env = clean_environment({
        "PAS_DATABASE_URL": "mysql+asyncmy://production/db", "PAS_TEST_MYSQL_URL": "mysql://existing/db",
        "PAS_ENCRYPTION_KEY": "existing-key", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "existing-secret",
        "HTTPS_PROXY": "http://proxy", "PYTHONPATH": "/existing/source", "PATH": "/usr/bin",
    })
    assert env == {"PATH": "/usr/bin"}


def test_optional_container_acceptance_is_explicit_and_does_not_enable_live_cloud():
    assert integration_environment(None, None, None) == {}
    assert integration_environment("pas-local:owned", "mysql:owned", "kindest/node:owned") == {
        "PAS_RUN_ENV_GENERATOR_MYSQL_TEST": "1", "PAS_TEST_IMAGE": "pas-local:owned",
        "PAS_RUN_KUBERNETES_INTEGRATION": "1", "PAS_TEST_MYSQL_IMAGE": "mysql:owned",
        "PAS_TEST_KIND_IMAGE": "kindest/node:owned",
    }


@pytest.mark.parametrize("candidate,mysql,kind", [(None, "mysql:owned", None), ("pas-local:owned", None, "kindest/node:owned")])
def test_optional_container_acceptance_requires_complete_image_selection(candidate, mysql, kind):
    with pytest.raises(ValueError, match="requires"):
        integration_environment(candidate, mysql, kind)
