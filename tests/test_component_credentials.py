"""The credential inventory the Helm chart is checked against.

`scripts/component_credentials.py` derives, from the settings classes, which
keys each Deployment may read out of the `Secret`. The chart job asserts the
rendered manifests match it exactly, so a blind spot here is a blind spot in
the chart -- silently, because nothing fails.
"""

from pydantic import SecretStr

from scripts.component_credentials import _is_credential, credentials


def test_an_optional_secret_is_still_a_secret() -> None:
    """The blind spot this file exists for.

    The first version tested `field.annotation is SecretStr`, which is False
    for `SecretStr | None`. Every optional credential was therefore exempt
    from the inventory -- and exempt from the chart's guard against a values
    file setting it as a plaintext env value. `sentry_dsn` sat outside it for
    exactly that reason.
    """
    assert _is_credential("anything", SecretStr | None)
    assert _is_credential("anything", SecretStr)


def test_a_plain_string_is_not_a_secret() -> None:
    """The other half: the check must not swallow ordinary configuration.

    Widening it far enough to catch `SecretStr | None` would be easy to
    overdo -- an inventory that lists every field is as useless as one that
    lists none, and it would put addresses into the SOPS rotation.
    """
    assert not _is_credential("outline_base_url", str)
    assert not _is_credential("health_port", int)
    assert not _is_credential("sentry_environment", str)


def test_database_url_is_a_credential_despite_being_a_plain_string() -> None:
    """It embeds the database password; the type says `str` only because
    SQLAlchemy takes one."""
    assert _is_credential("database_url", str)


def test_every_component_receives_the_sentry_dsn() -> None:
    """All three call `init_sentry` before reading their own settings, so all
    three need the key -- and the GitOps repository that supplies it is
    public, which is why it travels through the `Secret` rather than
    `commonEnv`. See docs/operations.md section 1.4."""
    for keys in credentials().values():
        assert "STURNUS_SENTRY_DSN" in keys


def test_link_never_receives_the_master_key() -> None:
    """`link` is the one component reachable from outside the cluster. The
    key that wraps every recording's data key must not be on it."""
    assert "STURNUS_MASTER_KEY" not in credentials()["sturnus-link"]
