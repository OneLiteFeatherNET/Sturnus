import tomllib
from pathlib import Path

import sturnus


def test_package_exposes_version() -> None:
    assert isinstance(sturnus.__version__, str)
    assert sturnus.__version__.count(".") == 2


def test_version_is_not_a_second_source_of_truth() -> None:
    """pyproject.toml is the only place the version is declared.

    Release Please rewrites it there and nowhere else, so the package must
    read its version from the installed metadata rather than carry a copy.
    A failure here after a version bump means the environment needs a
    re-sync, which is exactly the drift this guards against.
    """
    pyproject = tomllib.loads(Path(__file__).parent.parent.joinpath("pyproject.toml").read_text())
    declared: str = pyproject["project"]["version"]
    assert sturnus.__version__ == declared
