"""Static rules that stop a payload reaching a log line, checked over `src/`.

The runtime allowlist in `sturnus.observability.redaction` already drops an
unregistered field, so nothing here is the *only* thing standing between a
transcript and Loki. What these rules add is that a mistake fails the build
with a message naming the fix, instead of being silently dropped at runtime
and wondered about later -- and that the one thing the runtime allowlist
cannot police, the log *message* itself, stays a literal.

That last point is load-bearing beyond this repository:
`sturnus.infrastructure.observability.scrub_event` forwards
`logentry.message` -- `LogRecord.msg`, the format string as written in the
source -- to Sentry and nothing else. That is only safe while `msg` is a
literal. `log.error(f"failed for {transcript}")` would put the transcript
*into* `msg`, and no scrubbing hook can tell that apart from a template.
ruff's `G001`-`G004` (see `pyproject.toml`) forbid the f-string, `%`,
`.format()` and `+` spellings; rule R1 below covers the rest.

Modelled on `tests/test_architecture.py`, which already walks `src/` with
`ast` for exactly this kind of rule, and imports its name lists from
`sturnus.observability.fields` so the test and the runtime cannot drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sturnus.observability.fields import ALLOWED_FIELDS, DENIED_NAMES

SRC = Path(__file__).parent.parent / "src"

#: The module allowed to call `logging.basicConfig`. Anywhere else it would
#: install a second handler -- a second exit from the process, bypassing
#: `SturnusFilter` entirely.
_BASIC_CONFIG_OWNER = "setup.py"

_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
_EVENT_HELPERS = frozenset({"log_event", "log_exception"})

#: The names an exception is bound to in this repository. A denylist of
#: four spellings rather than a proof that a value is not an exception:
#: `except ValueError as exc` is the only way one gets a name at all, and
#: `exc` is what every `except` clause in `src/` calls it.
_EXCEPTION_NAMES = frozenset({"exc", "error", "exception", "err"})


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_log_call(node: ast.Call) -> bool:
    """`log.info(...)` and friends, but not `logger.log_something_else`."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _LOG_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id in {"log", "logger", "logging"}
    )


def _is_event_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Name) and func.id in _EVENT_HELPERS


def _direct_name(node: ast.expr) -> str | None:
    """The name of an expression passed *directly*, or `None`.

    Deliberately shallow. `len(body.encode("utf-8"))` is a `Call` and
    returns `None`, because taking the length of a body is exactly what the
    design asks call sites to do; `transcript` and `result.text` return
    their names, because passing those *is* the mistake. Keeping the rule
    shallow is what keeps its false-positive rate low enough that nobody
    is tempted to delete it -- the failure mode a name-based denylist
    actually dies of.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _message_argument(node: ast.Call) -> ast.expr | None:
    """The human-readable message argument of a log or event call."""
    if _is_event_call(node):
        # log_event(logger, level, event, message, ...)
        # log_exception(logger, level, event, message, exc, ...)
        return node.args[3] if len(node.args) > 3 else None
    if isinstance(node.func, ast.Attribute) and node.func.attr == "log":
        # log.log(level, message, ...)
        return node.args[1] if len(node.args) > 1 else None
    return node.args[0] if node.args else None


def _is_source_text(node: ast.expr) -> bool:
    """Whether this expression can only ever evaluate to text written here.

    A plain literal, implicit or explicit concatenation of literals, and a
    conditional choosing between two of them all qualify: every character
    that can reach `LogRecord.msg` is visible in the source and reviewable.
    Anything else -- a name, an f-string, a `.format()` -- does not.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_source_text(node.left) and _is_source_text(node.right)
    if isinstance(node, ast.IfExp):
        # `"recorded nothing" if empty else "closed"` -- both branches are
        # literals, so the set of possible messages is still a closed set
        # written in this file.
        return _is_source_text(node.body) and _is_source_text(node.orelse)
    return False


def test_r1_every_log_message_is_a_string_literal() -> None:
    """Rule R1. `logentry.message` is forwarded to Sentry; keep it source text."""
    violations: list[str] = []
    for path in _python_files():
        if path.name == "events.py":
            # `log_event`/`log_exception` are the helpers whose `message`
            # parameter this rule constrains at every *call* site; inside
            # them it is necessarily a variable.
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not (_is_log_call(node) or _is_event_call(node)):
                continue
            message = _message_argument(node)
            if message is None:
                continue
            if not _is_source_text(message):
                violations.append(
                    f"{path.relative_to(SRC)}:{node.lineno}: log message is not a string "
                    f"literal. Put the varying part in **fields instead."
                )
    assert not violations, "\n".join(violations)


def test_r2_no_denied_name_is_passed_to_a_log_call() -> None:
    """Rule R2. `log_event(..., transcript=result.text)` fails here, not silently."""
    violations: list[str] = []
    for path in _python_files():
        if path.name == "fields.py":
            continue  # the list itself
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not (_is_log_call(node) or _is_event_call(node)):
                continue
            for argument in node.args:
                name = _direct_name(argument)
                if name in DENIED_NAMES:
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: passes {name!r} to a log "
                        f"call. Log a count, a size or an id instead."
                    )
            for keyword in node.keywords:
                if keyword.arg in DENIED_NAMES:
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: log field {keyword.arg!r} "
                        f"is on the denied list in sturnus.observability.fields."
                    )
                name = _direct_name(keyword.value)
                if name in DENIED_NAMES:
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: log field "
                        f"{keyword.arg!r} is set from {name!r}, which carries payload."
                    )
    assert not violations, "\n".join(violations)


def test_r3_every_event_field_is_registered() -> None:
    """Rule R3. The registry is the review point for "we decided to log this"."""
    violations: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not _is_event_call(node):
                continue
            for keyword in node.keywords:
                if keyword.arg is None:
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: **kwargs into a log "
                        f"event hides which fields are emitted."
                    )
                    continue
                if keyword.arg not in ALLOWED_FIELDS:
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: {keyword.arg!r} is not in "
                        f"ALLOWED_FIELDS. Add it to sturnus.observability.fields, "
                        f"deliberately, or rename the field."
                    )
    assert not violations, "\n".join(violations)


def test_r4_basic_config_lives_in_exactly_one_module() -> None:
    """Rule R4. A second handler is a second, unfiltered way out of the process."""
    callers: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "basicConfig"
            ):
                callers.append(str(path.relative_to(SRC)))
    assert not callers, (
        "logging.basicConfig replaces nothing and appends an unfiltered handler; "
        f"call sturnus.observability.setup.configure_logging instead. Found in: {callers}"
    )


def test_r5_nothing_in_src_calls_print() -> None:
    """Rule R5. `print` writes to stdout without passing the filter at all."""
    violations: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                violations.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not violations, (
        f"print() bypasses SturnusFilter entirely; use log_event. Found at: {violations}"
    )


def test_r6_an_exception_is_never_interpolated_into_a_log_message() -> None:
    """Rule R6. `log.warning("failed: %s", exc)` prints `str(exc)` verbatim.

    Twelve call sites did exactly that before this branch, and one of them
    covered `_create_session_document`, which renders the assembled
    transcript through Jinja and posts it through httpx -- a
    `jinja2.UndefinedError` there can carry template context, and `%s` would
    print it. `log_exception` replaces the spelling: the type becomes a
    registered field, the traceback is rendered from static program text,
    and the message travels only if `SAFE_MESSAGE_TYPES` vouches for its
    class.
    """
    violations: list[str] = []
    for path in _python_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not _is_log_call(node):
                continue
            for argument in node.args[1:]:
                name = _direct_name(argument)
                if name in _EXCEPTION_NAMES:
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: interpolates {name!r} "
                        f"into a log message. Use log_exception(...) instead."
                    )
    assert not violations, "\n".join(violations)


def _formatting_log_helpers(module: ast.Module) -> frozenset[str]:
    """Functions in this module that render a message with `%` and then log it.

    `SturnusClient._notice` is the shape: it does `rendered = message %
    args` and then `log.warning("%s", rendered)`. Every character its
    callers hand it reaches `LogRecord.msg`'s output just as surely as if
    they had written the `log.warning` themselves -- but R1 sees a literal
    and R6 sees no `log.*` call, because one frame sits in between.

    Recognised by shape rather than by a list of blessed helper names: a
    function that renders a `%` expression and hands the result to a
    logger. A list of names is a list somebody has to remember to extend,
    which is the same failure this rule exists to stop.

    The `%` must actually *reach* the log call, whether inline or through
    one local name. Merely containing a `%` is not enough --
    `DaveDecryptor._note_failure` rate-limits itself with
    `self._consecutive_failures % FAILURE_LOG_EVERY` and passes its
    exception to `exc_info`, which is the correct handling and not a
    helper this rule has anything to say about.
    """
    helpers: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _renders_into_a_log_call(
            node
        ):
            helpers.add(node.name)
    return frozenset(helpers)


def _is_percent_format(node: ast.expr) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)


def _renders_into_a_log_call(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a `%` expression in this function ends up inside a log call."""
    rendered: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and _is_percent_format(node.value):
            rendered.update(
                name for name in (_direct_name(target) for target in node.targets) if name
            )
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_percent_format(node.value)
        ):
            name = _direct_name(node.target)
            if name:
                rendered.add(name)
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not _is_log_call(node):
            continue
        for argument in node.args:
            if _is_percent_format(argument) or _direct_name(argument) in rendered:
                return True
    return False


def _called_helper(node: ast.Call) -> str | None:
    """`_notice(...)` and `self._notice(...)`, but not `other._notice(...)`.

    Module-local on purpose. Following a helper across an import would
    need a resolver, and this rule earns its keep on the case that
    actually happens -- a private formatter beside the call sites that use
    it. A helper worth exporting is worth being `log_event`.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr if func.value.id == "self" else None
    return None


def test_r7_an_exception_never_reaches_a_helper_that_formats_and_logs() -> None:
    """Rule R7. One frame of indirection is not a way around R6.

    `self._notice(guild_id, "... (%s) ...", guild_id, exc)` renders
    `str(exc)` into the text that `log.warning` then emits, and
    `InvalidChannelList`'s message embeds the value it refused -- the
    stored `voice_channel_ids` text, put there by whoever ran the direct
    `UPDATE` that this notice exists to report. `routes_settings._write`
    already says in writing what to do instead, for the same exception
    class: the type and the key travel, the value does not.

    R6 catches only the direct spelling, so it caught none of this. A rule
    that matches one spelling is a rule the next helper walks past.
    """
    violations: list[str] = []
    for path in _python_files():
        module = _parse(path)
        helpers = _formatting_log_helpers(module)
        if not helpers:
            continue
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            helper = _called_helper(node)
            if helper is None or helper not in helpers:
                continue
            arguments = list(node.args) + [keyword.value for keyword in node.keywords]
            for argument in arguments:
                name = _direct_name(argument)
                if name in _EXCEPTION_NAMES:
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: passes {name!r} to "
                        f"{helper!r}, which renders its arguments into a log message. "
                        f"Pass type({name}).__name__ and the key instead."
                    )
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "module_source,flagged",
    [
        # The shape that got past R6: the literal is the helper's, the
        # exception is the caller's, and they meet one frame apart.
        (
            "def _notice(self, message, *args):\n"
            "    rendered = message % args\n"
            "    log.warning('%s', rendered)\n"
            "def caller(self):\n"
            "    try:\n"
            "        f()\n"
            "    except ValueError as exc:\n"
            "        self._notice('bad (%s)', exc)\n",
            True,
        ),
        # The fix. A type name is program text; the value stayed behind.
        (
            "def _notice(self, message, *args):\n"
            "    rendered = message % args\n"
            "    log.warning('%s', rendered)\n"
            "def caller(self):\n"
            "    try:\n"
            "        f()\n"
            "    except ValueError as exc:\n"
            "        self._notice('bad (%s)', type(exc).__name__)\n",
            False,
        ),
        # A helper that logs but never formats is not a formatter, and a
        # helper that formats but never logs is not a log call site.
        (
            "def _record(self, exc):\n"
            "    log.warning('failed')\n"
            "def caller(self):\n"
            "    try:\n"
            "        f()\n"
            "    except ValueError as exc:\n"
            "        self._record(exc)\n",
            False,
        ),
        # `%` on two integers is arithmetic. `DaveDecryptor._note_failure`
        # rate-limits itself with one and hands its exception to
        # `exc_info`, where the logging machinery renders the traceback --
        # the correct spelling, and one this rule must not shout at.
        (
            "def _note_failure(self, error):\n"
            "    if self.failures % 50 == 0:\n"
            "        log.warning('could not decrypt %d frames', self.failures, exc_info=error)\n"
            "def caller(self):\n"
            "    try:\n"
            "        f()\n"
            "    except ValueError as error:\n"
            "        self._note_failure(error)\n",
            False,
        ),
    ],
)
def test_r7_catches_what_it_claims_to(module_source: str, flagged: bool) -> None:
    """Shown to work on whole modules, because that is the scope it needs.

    R7 is the one rule here that cannot be demonstrated on a single
    expression: the mistake is a relationship between two of them.
    """
    module = ast.parse(module_source)
    helpers = _formatting_log_helpers(module)
    found = any(
        isinstance(node, ast.Call)
        and _called_helper(node) in helpers
        and any(_direct_name(argument) in _EXCEPTION_NAMES for argument in node.args)
        for node in ast.walk(module)
    )
    assert found is flagged


@pytest.mark.parametrize(
    "source,rule_violated",
    [
        ('log.info("transcribed %s", text)', "r2"),
        ('log.info("transcribed %s", result.text)', "r2"),
        ('log_event(log, 1, E.X, "done", transcript=body)', "r2"),
        ('log_event(log, 1, E.X, "done", token=t)', "r2"),
        ('log.warning("failed: %s", exc)', "r6"),
        ('log_event(log, 1, E.X, "done", job_id=7)', None),
        ('log_event(log, 1, E.X, "done", body_bytes=len(body))', None),
        ('log.info("connected to %d guilds", count)', None),
    ],
)
def test_the_rules_catch_what_they_claim_to(source: str, rule_violated: str | None) -> None:
    """A table of spellings, so the rules are shown to work rather than trusted.

    Mirrors `test_import_resolution_comprehensive` in
    `tests/test_architecture.py`: the point is that a future contributor can
    read what is and is not allowed without reverse-engineering the walker.
    """
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.Expr)
    call = node.value
    assert isinstance(call, ast.Call)

    r2 = any(_direct_name(a) in DENIED_NAMES for a in call.args) or any(
        kw.arg in DENIED_NAMES or _direct_name(kw.value) in DENIED_NAMES for kw in call.keywords
    )
    r6 = _is_log_call(call) and any(
        _direct_name(a) in {"exc", "error", "exception", "err"} for a in call.args[1:]
    )

    if rule_violated == "r2":
        assert r2, f"R2 should have flagged: {source}"
    elif rule_violated == "r6":
        assert r6, f"R6 should have flagged: {source}"
    else:
        assert not r2 and not r6, f"nothing should have flagged: {source}"
