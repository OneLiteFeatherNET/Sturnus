"""The vocabulary and the redaction path every telemetry channel shares.

Sturnus emits telemetry into three retained stores -- Loki (pod logs, via
`alloy-logs`), Tempo (spans, via `alloy-receiver`), and Sentry (errors,
`sturnus.infrastructure.observability`). Spec 15 treats the recordings as
the most consequential data in the system and
`docs/verification/end-to-end-checklist.md` makes "no transcript, audio,
token or key appears in any pod log" a blocking legal gate.

Three stores with three redaction implementations is worse than one store
with none: each would be correct about a slightly different set of names,
and the gap between them is where a transcript gets out. So this package
holds **one** field registry (`fields.ALLOWED_FIELDS`) and **one**
scrubbing function (`redaction.scrub_fields`), and every channel is built
on top of them:

- log records, through `events.log_event` and `redaction.SturnusFilter`;
- span and metric attributes, through
  `sturnus.infrastructure.telemetry.span` / `_metric_attributes`;
- Sentry exception messages, through `redaction.SAFE_MESSAGE_TYPES`, which
  `sturnus.infrastructure.observability.SAFE_VALUE_TYPES` re-exports rather
  than restating.

Adding a field is therefore one edit, in one file, that shows up in review
as "we decided to put this in Loki, Tempo and Grafana" -- which is what it
is.

**Standard library only, deliberately.** `sturnus.application` already uses
stdlib `logging` in four modules and must be able to call `log_event`; the
architecture rule in `tests/test_architecture.py` forbids it importing
third-party packages, and `tests/observability/test_package_boundaries.py`
holds this package to the same standard so that importing it from
`application` can never smuggle OpenTelemetry in behind it. The
OpenTelemetry SDK lives in `sturnus.infrastructure.telemetry` and imports
*this* package, never the other way round.
"""

from __future__ import annotations
