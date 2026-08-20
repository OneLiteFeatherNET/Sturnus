"""Assert uv.lock pins the same version of this project as pyproject.toml.

Parses both files as TOML instead of matching lines: the project version is
`[project].version` and the lock's is the `version` of the `[[package]]`
entry named after the project, and neither is "the first version-looking
line in the file".
"""

import sys
import tomllib
from pathlib import Path


def main() -> int:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    name = project["project"]["name"]
    declared = project["project"].get("version")
    if declared is None:
        # A `dynamic = ["version"]` project has no static version for
        # release-please's marker comment to sit on either, so the whole
        # arrangement this guards would need rethinking, not just this check.
        print(
            "::error::pyproject.toml declares no static [project] version. "
            "release-please writes the version through a marker comment on "
            "that line, so both it and this check need revisiting."
        )
        return 1

    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    entries = [p for p in lock.get("package", []) if p.get("name") == name]
    if len(entries) != 1:
        print(
            f"::error::uv.lock holds {len(entries)} [[package]] entries named "
            f"{name!r}, expected exactly one. The lockfile layout changed and "
            f"this check needs updating."
        )
        return 1

    locked = entries[0].get("version")
    print(f"pyproject.toml={declared} uv.lock={locked}")
    if locked != declared:
        print(
            f"::error::uv.lock pins {name} at {locked} but pyproject.toml says "
            f"{declared}. On a release PR this means release-please did not "
            f"rewrite uv.lock -- check the uv.lock extra-files entry in "
            f"release-please-config.json. Otherwise run 'uv lock' and commit "
            f"the result."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
