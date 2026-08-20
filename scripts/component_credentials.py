"""Print the credentials each component's settings class declares.

The Helm chart hands every Deployment its credentials one key at a time
(`sturnus.secretEnv` in charts/sturnus/templates/_helpers.tpl), which means
the chart holds a list that has to agree with the code. This is what CI
compares that list against, so the two cannot drift apart silently: add a
`SecretStr` field to a settings class without giving the component the key,
and the chart job fails naming it.

A credential is a `SecretStr` field, plus `database_url` -- typed `str`
because SQLAlchemy takes a string, but it embeds the database password, so
it is stored and handled like the rest.
"""

import json
import sys

from pydantic import SecretStr

from sturnus.config import Settings
from sturnus.entrypoints.link import LinkSettings
from sturnus.entrypoints.worker import WorkerSettings

#: Keyed by the Deployment name the chart renders, so the caller can compare
#: without translating.
COMPONENTS = {
    "sturnus-bot": Settings,
    "sturnus-worker": WorkerSettings,
    "sturnus-link": LinkSettings,
}

#: Not a SecretStr, but a credential all the same -- see the module docstring.
ALSO_SECRET = {"database_url"}


def credentials() -> dict[str, list[str]]:
    out = {}
    for deployment, cls in COMPONENTS.items():
        names = [
            f"STURNUS_{name.upper()}"
            for name, field in cls.model_fields.items()
            if field.annotation is SecretStr or name in ALSO_SECRET
        ]
        out[deployment] = sorted(names)
    return out


if __name__ == "__main__":
    json.dump(credentials(), sys.stdout, indent=2, sort_keys=True)
    print()
