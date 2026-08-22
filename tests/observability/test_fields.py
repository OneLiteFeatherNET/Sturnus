"""The field registry, and what must never drift away from it."""

from sturnus.observability.fields import COMPONENTS


def test_every_process_that_configures_logging_is_named_in_components() -> None:
    """The tuple drifted once, and nothing caught it.

    `api` was added as a fourth process -- calling `configure_logging`,
    `init_sentry` and `init_telemetry` exactly as the other three do -- and
    `COMPONENTS` was not updated, for the length of a whole change. The
    consequence is not cosmetic: `service_name` derives from this, so a
    process missing here reports a `service.name` that agrees with nothing
    in Tempo, and an operator correlating a Sentry issue with a trace finds
    neither.

    Derived from `[project.scripts]` rather than restated, so a fifth
    process cannot arrive the same way. The console is absent from both --
    it is a Node process that calls none of this.
    """
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(
        (Path(__file__).parent.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    entrypoints = {name.removeprefix("sturnus-") for name in pyproject["project"]["scripts"]}
    assert entrypoints == set(COMPONENTS), (
        f"[project.scripts] declares {sorted(entrypoints)} but COMPONENTS names "
        f"{sorted(COMPONENTS)}; a process missing here reports a service.name "
        "that agrees with nothing"
    )
