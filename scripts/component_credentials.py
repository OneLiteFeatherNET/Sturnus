"""Print the credentials each component's settings class declares.

The Helm chart hands every Deployment its credentials one key at a time
(`sturnus.secretEnv` in charts/sturnus/templates/_helpers.tpl), which means
the chart holds a list that has to agree with the code. This is what CI
compares that list against, so the two cannot drift apart silently: add a
`SecretStr` field to a settings class without giving the component the key,
and the chart job fails naming it.

A credential is a `SecretStr` field -- including an optional one, typed
`SecretStr | None` -- plus `database_url`, typed `str` because SQLAlchemy
takes a string, but it embeds the database password, so it is stored and
handled like the rest.

`SentrySettings` is folded into every component that has a settings class.
It is a class of its own because they all read it (see its docstring), so
unlike the others it is not "one component's settings"; but the DSN still
has to reach each container through the Secret, and this list is what the
chart is checked against.

The console has no settings class and no credentials, and is listed anyway
with an empty list -- see `CREDENTIAL_FREE`.
"""

import json
import sys
from typing import get_args

from pydantic import SecretStr
from pydantic_settings import BaseSettings

from sturnus.config import SentrySettings, Settings
from sturnus.entrypoints.api import ApiSettings
from sturnus.entrypoints.link import LinkSettings
from sturnus.entrypoints.worker import WorkerSettings

#: Keyed by the Deployment name the chart renders, so the caller can compare
#: without translating. Annotated so the values are settings classes rather
#: than pydantic's metaclass, which is what mypy infers from a bare literal.
COMPONENTS: dict[str, type[BaseSettings]] = {
    "sturnus-bot": Settings,
    "sturnus-worker": WorkerSettings,
    "sturnus-link": LinkSettings,
    "sturnus-api": ApiSettings,
}

#: Components with no settings class of their own, and no credentials at
#: all. Listed rather than omitted: a component missing from this file
#: would fail the chart job as "rendered but not expected", which reads as
#: an oversight. Naming it with an empty list says the emptiness is the
#: design -- the console renders pages and calls the API, and every
#: credential lives on the other side of that boundary.
CREDENTIAL_FREE = ("sturnus-console",)

#: Not a SecretStr, but a credential all the same -- see the module docstring.
ALSO_SECRET = {"database_url"}


def _is_credential(name: str, annotation: object) -> bool:
    """Whether one settings field holds a credential.

    `SecretStr | None` is a `SecretStr` for this purpose. Testing the
    annotation with `is` alone missed it, which is how the Sentry DSN sat
    outside the chart's credential inventory: an optional secret is still a
    secret, and a check that only recognises the required spelling silently
    exempts every optional one added after it.
    """
    if name in ALSO_SECRET:
        return True
    return SecretStr in get_args(annotation) or annotation is SecretStr


def credentials() -> dict[str, list[str]]:
    #: Read by all three components, so it contributes to each list rather
    #: than forming one of its own.
    shared = [
        f"STURNUS_{name.upper()}"
        for name, field in SentrySettings.model_fields.items()
        if _is_credential(name, field.annotation)
    ]
    out = {}
    for deployment, cls in COMPONENTS.items():
        names = [
            f"STURNUS_{name.upper()}"
            for name, field in cls.model_fields.items()
            if _is_credential(name, field.annotation)
        ]
        out[deployment] = sorted(names + shared)
    for deployment in CREDENTIAL_FREE:
        out[deployment] = []
    return out


if __name__ == "__main__":
    json.dump(credentials(), sys.stdout, indent=2, sort_keys=True)
    print()
