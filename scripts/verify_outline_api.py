"""Live verification of the Outline API assumptions the adapters are built on.

Run this **once, by hand, against a real Outline instance**, before the
Outline adapters ship. It exercises the exact HTTP calls
`sturnus.infrastructure.documents.outline.OutlineSink` and
`sturnus.infrastructure.documents.outline_oauth.OutlineOAuth` make, prints
the real request and response for each, and reports every assumption
recorded in `docs/verification/outline-api.md` and
`docs/verification/outline-oauth.md` as CONFIRMED, CONTRADICTED, or
SKIPPED (skipped only for the two OAuth checks that need a manual browser
step -- see "OAuth token exchange" below).

Usage::

    uv run scripts/verify_outline_api.py \\
        --base-url https://notes.example.com \\
        --token $OUTLINE_API_TOKEN \\
        --collection-id 5b1c...-scratch-collection

Add ``--oauth-client-id`` and ``--oauth-redirect-uri`` to also probe the
OAuth authorize route, and ``--oauth-client-secret``/``--oauth-code`` (the
``code`` query parameter from the redirect after you approve the printed
authorize URL in a browser) to run the full token exchange and re-check the
identity endpoint with a genuine OAuth-issued access token rather than a
personal API token.

**Safety**

- The Outline API token is redacted everywhere it could appear in this
  script's output (headers, request bodies, error text) -- the output is
  meant to be safe to paste directly into a pull request. The same applies
  to an OAuth client secret and any access token this script obtains.
  Redaction is best-effort string substitution: double-check before
  pasting into a public or wide-audience location regardless.
- This script only ever creates a document in the collection id you pass
  via ``--collection-id``. Pass a scratch/throwaway collection, never a
  real one -- see that flag's help text. The two failure-path checks
  (invalid token, non-existent collection) never succeed in creating
  anything, by construction.
- Documents created by this script are **not** deleted automatically. Clean
  up the scratch collection afterward if you care to.

**Exit status**: non-zero if any assumption is reported CONTRADICTED or if
a check could not complete (ERROR) -- suitable for gating a deployment on.
A SKIPPED check does not affect the exit status.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

Verdict = Literal["CONFIRMED", "CONTRADICTED", "SKIPPED", "ERROR"]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_PERMANENT_STATUS_CODES = frozenset({401, 403, 404})


@dataclass
class Finding:
    """One assumption from `docs/verification/outline-*.md`, checked against reality."""

    source: str
    assumption: str
    verdict: Verdict
    detail: str


@dataclass
class Redactor:
    """Replaces secret values with `<redacted>` in anything printed to stdout.

    Secrets are registered as they become known (the token up front, an
    OAuth client secret and access token once the OAuth exchange runs) so
    that every later print call redacts them too.
    """

    secrets: list[str] = field(default_factory=list)

    def add(self, secret: str | None) -> None:
        if secret:
            self.secrets.append(secret)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        return _EMAIL_RE.sub("<redacted-email>", text)


def _print_exchange(
    redact: Redactor,
    *,
    label: str,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
    response: httpx.Response | None = None,
) -> None:
    print(f"\n--- {label}: {method} {redact(url)} ---")
    if headers:
        print("request headers:")
        print(redact(json.dumps(headers, indent=2)))
    if json_body is not None:
        print("request json body:")
        print(redact(json.dumps(json_body, indent=2)))
    if form_body is not None:
        print("request form body:")
        print(redact(json.dumps(form_body, indent=2)))
    if response is not None:
        print(f"response status: {response.status_code}")
        try:
            body = json.dumps(response.json(), indent=2)
        except ValueError:
            body = response.text
        print("response body:")
        print(redact(body))


def check_create_document(
    client: httpx.Client, redact: Redactor, *, token: str, collection_id: str
) -> tuple[list[Finding], str | None, str | None]:
    """Exercises `OutlineSink.create`: `POST /api/documents.create`.

    Checks outline-api.md's assumptions #1 (endpoint path), #2 (request
    field names), and #3 (response shape, including relative-vs-absolute
    URL). Returns the findings plus the created document's id and url, so
    later output can reference them.
    """
    title = f"Sturnus verification scratch doc {datetime.now(UTC).isoformat()}"
    payload = {
        "title": title,
        "text": "Created by `scripts/verify_outline_api.py`. Safe to delete.",
        "collectionId": collection_id,
        "publish": True,
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = client.post("/api/documents.create", json=payload, headers=headers)
    except httpx.RequestError as exc:
        _print_exchange(
            redact,
            label="create document",
            method="POST",
            url="/api/documents.create",
            headers=headers,
            json_body=payload,
        )
        print(f"request failed before a response arrived: {exc!r}")
        return (
            [
                Finding(
                    "outline-api.md #1",
                    "endpoint path (/api/documents.create)",
                    "ERROR",
                    f"request failed: {exc!r}",
                )
            ],
            None,
            None,
        )

    _print_exchange(
        redact,
        label="create document",
        method="POST",
        url="/api/documents.create",
        headers=headers,
        json_body=payload,
        response=response,
    )

    findings: list[Finding] = []
    if response.status_code != httpx.codes.OK:
        findings.append(
            Finding(
                "outline-api.md #1",
                "endpoint path (/api/documents.create)",
                "CONTRADICTED",
                f"expected 200 for a valid create request, got {response.status_code}",
            )
        )
        findings.append(
            Finding(
                "outline-api.md #2",
                "request field names (title/text/collectionId/publish)",
                "ERROR",
                "create request did not succeed; field names could not be confirmed",
            )
        )
        findings.append(
            Finding(
                "outline-api.md #3",
                "response shape (data.id / data.url)",
                "ERROR",
                "no successful response to inspect",
            )
        )
        return findings, None, None

    findings.append(
        Finding(
            "outline-api.md #1",
            "endpoint path (/api/documents.create)",
            "CONFIRMED",
            "POST returned 200",
        )
    )
    findings.append(
        Finding(
            "outline-api.md #4",
            "auth header format (Authorization: Bearer <token>)",
            "CONFIRMED",
            "the request authenticated successfully with this header",
        )
    )

    try:
        body: dict[str, Any] = response.json()
        data = body["data"]
        document_id = str(data["id"])
        url = str(data["url"])
    except (ValueError, KeyError, TypeError) as exc:
        findings.append(
            Finding(
                "outline-api.md #2",
                "request field names (title/text/collectionId/publish)",
                "CONFIRMED",
                "server accepted the payload (200) and created a document",
            )
        )
        findings.append(
            Finding(
                "outline-api.md #3",
                "response shape (data.id / data.url)",
                "CONTRADICTED",
                f"could not read data.id / data.url from the response: {exc!r}",
            )
        )
        return findings, None, None

    findings.append(
        Finding(
            "outline-api.md #2",
            "request field names (title/text/collectionId/publish)",
            "CONFIRMED",
            "server accepted the payload (200) and created a document",
        )
    )
    findings.append(
        Finding(
            "outline-api.md #3",
            "response shape (data.id / data.url)",
            "CONFIRMED",
            "response has a top-level data object with id and url",
        )
    )

    if url.startswith(("http://", "https://")):
        findings.append(
            Finding(
                "outline-api.md #3",
                "returned url is relative, not absolute",
                "CONTRADICTED",
                f"url field is already absolute ({url!r}). Note: "
                "_extract_created_document already handles this case (it passes an "
                "already-absolute url through unchanged), so this does not require a "
                "code change -- but the assumption as documented does not hold, and "
                "the doc should be updated to say so.",
            )
        )
    else:
        findings.append(
            Finding(
                "outline-api.md #3",
                "returned url is relative, not absolute",
                "CONFIRMED",
                f"url field is a relative path ({url!r}), matches the assumption",
            )
        )

    return findings, document_id, url


def check_invalid_token(
    client: httpx.Client, redact: Redactor, *, collection_id: str
) -> list[Finding]:
    """A request with a bogus token: checks outline-api.md assumption #5 (invalid token)."""
    bogus_token = f"invalid-{uuid.uuid4()}"
    payload = {
        "title": "should never be created (invalid token check)",
        "text": "should never be created",
        "collectionId": collection_id,
        "publish": True,
    }
    headers = {"Authorization": f"Bearer {bogus_token}"}
    try:
        response = client.post("/api/documents.create", json=payload, headers=headers)
    except httpx.RequestError as exc:
        _print_exchange(
            redact,
            label="invalid token",
            method="POST",
            url="/api/documents.create",
            headers=headers,
            json_body=payload,
        )
        print(f"request failed before a response arrived: {exc!r}")
        return [
            Finding(
                "outline-api.md #5",
                "invalid-token failure status code (assumed 401/403)",
                "ERROR",
                f"request failed: {exc!r}",
            )
        ]

    _print_exchange(
        redact,
        label="invalid token",
        method="POST",
        url="/api/documents.create",
        headers=headers,
        json_body=payload,
        response=response,
    )

    if response.status_code in _PERMANENT_STATUS_CODES:
        return [
            Finding(
                "outline-api.md #5",
                "invalid-token failure status code (assumed 401/403)",
                "CONFIRMED",
                f"status {response.status_code} is in the adapter's permanent-failure set "
                f"{sorted(_PERMANENT_STATUS_CODES)}",
            )
        ]
    return [
        Finding(
            "outline-api.md #5",
            "invalid-token failure status code (assumed 401/403)",
            "CONTRADICTED",
            f"got status {response.status_code}, which is NOT in the adapter's "
            f"permanent-failure set {sorted(_PERMANENT_STATUS_CODES)}. If this status is "
            "not a 2xx, `_PERMANENT_STATUS_CODES` in outline.py needs this code added, or "
            "the adapter will retry a request that can never succeed.",
        )
    ]


def check_missing_collection(
    client: httpx.Client, redact: Redactor, *, token: str
) -> list[Finding]:
    """A request against a collection id that does not exist.

    Checks outline-api.md assumption #5's second half, called out there as
    "the second-highest-risk assumption in this file": a 400 or a 200 with
    an error payload here would fall through to `raise_for_status()` /
    `.json()` and be misclassified as retryable or crash on `KeyError`.
    """
    missing_collection_id = str(uuid.uuid4())
    payload = {
        "title": "should never be created (missing collection check)",
        "text": "should never be created",
        "collectionId": missing_collection_id,
        "publish": True,
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = client.post("/api/documents.create", json=payload, headers=headers)
    except httpx.RequestError as exc:
        _print_exchange(
            redact,
            label="non-existent collection",
            method="POST",
            url="/api/documents.create",
            headers=headers,
            json_body=payload,
        )
        print(f"request failed before a response arrived: {exc!r}")
        return [
            Finding(
                "outline-api.md #5",
                "non-existent-collection failure status code (assumed 404)",
                "ERROR",
                f"request failed: {exc!r}",
            )
        ]

    _print_exchange(
        redact,
        label="non-existent collection",
        method="POST",
        url="/api/documents.create",
        headers=headers,
        json_body=payload,
        response=response,
    )

    if response.status_code == httpx.codes.NOT_FOUND:
        return [
            Finding(
                "outline-api.md #5",
                "non-existent-collection failure status code (assumed 404)",
                "CONFIRMED",
                "status 404, matches the assumption and is in the adapter's permanent-failure set",
            )
        ]
    in_permanent_set = response.status_code in _PERMANENT_STATUS_CODES
    return [
        Finding(
            "outline-api.md #5",
            "non-existent-collection failure status code (assumed 404)",
            "CONTRADICTED",
            f"got status {response.status_code}, not 404. "
            + (
                "It is still in the adapter's permanent-failure set, so the retryable/"
                "permanent distinction happens to still be correct, but the documented "
                "assumption should be corrected."
                if in_permanent_set
                else "It is NOT in the adapter's permanent-failure set "
                f"{sorted(_PERMANENT_STATUS_CODES)} -- this is exactly the 'queue that "
                "never drains' failure mode outline-api.md warns about: a non-existent "
                "collection would be retried forever. `_PERMANENT_STATUS_CODES` needs "
                "this status added, or the response must be inspected for a 200-with-"
                "error-payload shape before this ships."
            ),
        )
    ]


def check_identity_endpoint(
    client: httpx.Client, redact: Redactor, *, token: str, label: str = "personal API token"
) -> tuple[list[Finding], str | None]:
    """`POST /api/auth.info`: outline-oauth.md assumptions #2 (path) and #5 (user id field).

    Called with the plain `--token` this script was given (a personal API
    token) so this check needs no OAuth flow to run. This confirms the
    endpoint and response shape, and which field holds the user id -- but
    NOT that an OAuth-issued access token is accepted identically; that
    part of the assumption is only confirmed by `check_oauth_token_exchange`
    below, when a real OAuth code is supplied.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = client.post("/api/auth.info", json={}, headers=headers)
    except httpx.RequestError as exc:
        _print_exchange(
            redact,
            label=f"identity ({label})",
            method="POST",
            url="/api/auth.info",
            headers=headers,
            json_body={},
        )
        print(f"request failed before a response arrived: {exc!r}")
        return (
            [
                Finding(
                    "outline-oauth.md #2/#5",
                    f"identity endpoint ({label})",
                    "ERROR",
                    f"request failed: {exc!r}",
                )
            ],
            None,
        )

    _print_exchange(
        redact,
        label=f"identity ({label})",
        method="POST",
        url="/api/auth.info",
        headers=headers,
        json_body={},
        response=response,
    )

    if response.status_code != httpx.codes.OK:
        return (
            [
                Finding(
                    "outline-oauth.md #2",
                    f"identity endpoint path, POST /api/auth.info ({label})",
                    "CONTRADICTED",
                    f"expected 200, got {response.status_code}",
                ),
                Finding(
                    "outline-oauth.md #5",
                    f"identity response shape / user id field ({label})",
                    "ERROR",
                    "identity request did not succeed; response shape could not be checked",
                ),
            ],
            None,
        )

    findings = [
        Finding(
            "outline-oauth.md #2",
            f"identity endpoint path, POST /api/auth.info ({label})",
            "CONFIRMED",
            "POST returned 200",
        )
    ]
    try:
        body: dict[str, Any] = response.json()
        user = body["data"]["user"]
        user_id = str(user["id"])
        _ = user["name"]
    except (ValueError, KeyError, TypeError) as exc:
        findings.append(
            Finding(
                "outline-oauth.md #5",
                f"identity response shape / user id field ({label})",
                "CONTRADICTED",
                f"could not read data.user.id / data.user.name: {exc!r}. This is the "
                "highest-risk assumption in outline-oauth.md -- `_extract_identity` in "
                "outline_oauth.py must be updated to match the real shape before this "
                "ships, or every mention this bot creates resolves to nobody, silently.",
            )
        )
        return findings, None

    findings.append(
        Finding(
            "outline-oauth.md #5",
            f"identity response shape / user id field ({label})",
            "CONFIRMED",
            f"data.user.id is present ({user_id!r}) and data.user.name is present -- "
            "matches _extract_identity's assumption",
        )
    )
    return findings, user_id


def check_oauth_authorize_path(
    redact: Redactor, *, base_url: str, args: argparse.Namespace
) -> list[Finding]:
    """`GET /oauth/authorize`: outline-oauth.md assumption #2's authorize half.

    Only run when `--oauth-client-id` and `--oauth-redirect-uri` are given
    -- the authorize route needs no client secret, so this much can be
    checked without a manual browser step. A 404 here means the route
    itself doesn't exist on this instance (e.g. the OAuth provider feature
    is disabled or not yet shipped); any other status means the route
    exists and rendered something (a consent screen, a login redirect, a
    validation error) -- distinguishing those requires a human in a
    browser, which is why this check stops at "the route exists".
    """
    if not (args.oauth_client_id and args.oauth_redirect_uri):
        return [
            Finding(
                "outline-oauth.md #2",
                "authorize endpoint path (GET /oauth/authorize)",
                "SKIPPED",
                "pass --oauth-client-id and --oauth-redirect-uri to check this",
            )
        ]

    query = (
        f"client_id={args.oauth_client_id}&redirect_uri={args.oauth_redirect_uri}"
        "&response_type=code&scope=read&state=verify-scratch-state"
    )
    url = f"{base_url}/oauth/authorize?{query}"
    print(f"\nauthorize URL (open in a browser to complete the OAuth flow by hand):\n{url}")
    try:
        with httpx.Client(follow_redirects=False, timeout=30.0) as probe:
            response = probe.get(url)
    except httpx.RequestError as exc:
        print(f"GET to the authorize URL failed before a response arrived: {exc!r}")
        return [
            Finding(
                "outline-oauth.md #2",
                "authorize endpoint path (GET /oauth/authorize)",
                "ERROR",
                f"request failed: {exc!r}",
            )
        ]

    _print_exchange(
        redact,
        label="oauth authorize (route existence only)",
        method="GET",
        url=url,
        response=response,
    )
    if response.status_code == httpx.codes.NOT_FOUND:
        return [
            Finding(
                "outline-oauth.md #2",
                "authorize endpoint path (GET /oauth/authorize)",
                "CONTRADICTED",
                "GET returned 404 -- the route does not exist at this path on this "
                "instance. Confirm the OAuth provider feature is enabled and the "
                "instance version supports it (see outline-oauth.md Finding 1) before "
                "assuming the path itself is merely wrong.",
            )
        ]
    return [
        Finding(
            "outline-oauth.md #2",
            "authorize endpoint path (GET /oauth/authorize)",
            "CONFIRMED",
            f"GET returned {response.status_code} (not 404), the route exists. This "
            "does not confirm the consent screen or redirect behaves correctly -- "
            "complete the flow in a browser and pass the resulting code via "
            "--oauth-code to check the token exchange too.",
        )
    ]


def check_oauth_token_exchange(
    client: httpx.Client, redact: Redactor, *, base_url: str, args: argparse.Namespace
) -> list[Finding]:
    """`POST /oauth/token` then `POST /api/auth.info` with the resulting access token.

    outline-oauth.md assumptions #3/#4 (token endpoint request/response
    shape, client_secret in the body) and the OAuth half of #5 (does an
    OAuth-issued token work identically to a personal API token against
    `auth.info`). Requires a real authorization `code`, which only exists
    after a human completes the consent screen printed by
    `check_oauth_authorize_path` in a browser -- there is no way to obtain
    one without that manual step, so this check is SKIPPED until
    `--oauth-code` is supplied.
    """
    if not (args.oauth_client_id and args.oauth_client_secret and args.oauth_redirect_uri):
        return [
            Finding(
                "outline-oauth.md #3/#4",
                "token endpoint request/response shape",
                "SKIPPED",
                "pass --oauth-client-id, --oauth-client-secret and --oauth-redirect-uri "
                "to check this",
            ),
            Finding(
                "outline-oauth.md #5",
                "identity endpoint accepts an OAuth-issued token",
                "SKIPPED",
                "same reason",
            ),
        ]
    if not args.oauth_code:
        return [
            Finding(
                "outline-oauth.md #3/#4",
                "token endpoint request/response shape",
                "SKIPPED",
                "visit the authorize URL printed above in a browser, approve, and pass "
                "the 'code' query parameter from the redirect as --oauth-code, then "
                "rerun",
            ),
            Finding(
                "outline-oauth.md #5",
                "identity endpoint accepts an OAuth-issued token",
                "SKIPPED",
                "same reason",
            ),
        ]

    redact.add(args.oauth_client_secret)
    redact.add(args.oauth_code)
    form_body = {
        "grant_type": "authorization_code",
        "code": args.oauth_code,
        "redirect_uri": args.oauth_redirect_uri,
        "client_id": args.oauth_client_id,
        "client_secret": args.oauth_client_secret,
    }
    try:
        response = client.post(f"{base_url}/oauth/token", data=form_body)
    except httpx.RequestError as exc:
        _print_exchange(
            redact,
            label="oauth token exchange",
            method="POST",
            url="/oauth/token",
            form_body=form_body,
        )
        print(f"request failed before a response arrived: {exc!r}")
        return [
            Finding(
                "outline-oauth.md #3/#4",
                "token endpoint request/response shape",
                "ERROR",
                f"request failed: {exc!r}",
            )
        ]

    # Register the access token with the redactor BEFORE anything prints the
    # response body. This endpoint is the one place a live token appears in
    # output the operator is told is safe to paste into a pull request, so the
    # ordering here is the whole guarantee. Parsing is best-effort: a malformed
    # body is handled properly further down, and this only needs to catch the
    # token when there is one.
    with contextlib.suppress(ValueError, KeyError, TypeError):
        redact.add(str(response.json()["access_token"]))

    _print_exchange(
        redact,
        label="oauth token exchange",
        method="POST",
        url="/oauth/token",
        form_body=form_body,
        response=response,
    )

    if response.status_code != httpx.codes.OK:
        return [
            Finding(
                "outline-oauth.md #3/#4",
                "token endpoint request/response shape",
                "CONTRADICTED",
                f"expected 200, got {response.status_code} -- see outline-oauth.md "
                "Finding 8 for the assumed error shape (400 / invalid_grant) if that's "
                "what came back",
            )
        ]

    findings: list[Finding] = []
    try:
        token_body: dict[str, Any] = response.json()
        access_token = str(token_body["access_token"])
    except (ValueError, KeyError, TypeError) as exc:
        findings.append(
            Finding(
                "outline-oauth.md #3/#4",
                "token endpoint request/response shape",
                "CONTRADICTED",
                f"200 response did not contain an access_token field readable as expected: {exc!r}",
            )
        )
        return findings

    redact.add(access_token)  # already registered above; kept for the parsed value
    findings.append(
        Finding(
            "outline-oauth.md #3/#4",
            "token endpoint request/response shape",
            "CONFIRMED",
            "client_secret in the POST body was accepted, and the 200 response "
            "contains an access_token field",
        )
    )

    identity_findings, _ = check_identity_endpoint(
        client, redact, token=access_token, label="OAuth-issued access token"
    )
    for finding in identity_findings:
        if finding.assumption.startswith("identity endpoint path"):
            finding.source = "outline-oauth.md #5"
            finding.assumption = "identity endpoint accepts an OAuth-issued token"
        findings.append(finding)
    return findings


def _print_report(findings: list[Finding], redact: Redactor) -> int:
    # Detail strings are built from response bodies and exception text, so they
    # go through the redactor too. No current detail carries a secret, but the
    # guarantee this script makes to the operator — that its output is safe to
    # paste into a pull request — has to hold structurally, not by inspection.
    print("\n" + "=" * 78)
    print("ASSUMPTION VERIFICATION REPORT")
    print("=" * 78)
    width = max((len(f.assumption) for f in findings), default=0)
    for f in findings:
        print(f"[{f.verdict:<12}] ({f.source}) {f.assumption.ljust(width)}")
        print(f"             {redact(f.detail)}")
    verdicts: tuple[Verdict, ...] = ("CONFIRMED", "CONTRADICTED", "SKIPPED", "ERROR")
    counts = {v: sum(1 for f in findings if f.verdict == v) for v in verdicts}
    print("-" * 78)
    print(
        f"confirmed={counts['CONFIRMED']} contradicted={counts['CONTRADICTED']} "
        f"skipped={counts['SKIPPED']} error={counts['ERROR']}"
    )
    if counts["CONTRADICTED"] or counts["ERROR"]:
        print(
            "\nAt least one assumption is CONTRADICTED or could not be checked (ERROR). "
            "Update the adapter and docs/verification/outline-*.md before this ships."
        )
        return 1
    print("\nAll checked assumptions were confirmed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the Outline API assumptions in docs/verification/outline-api.md and "
            "docs/verification/outline-oauth.md against a real Outline instance. Exercises "
            "the exact calls OutlineSink and OutlineOAuth make and reports each documented "
            "assumption as CONFIRMED, CONTRADICTED, SKIPPED, or ERROR."
        ),
        epilog=(
            "This script WILL create a real (throwaway) document in the collection given "
            "by --collection-id. Pass a scratch collection, never a real one. The token is "
            "redacted in all printed output."
        ),
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL of the Outline instance, e.g. https://notes.example.com",
    )
    parser.add_argument(
        "--token",
        required=True,
        help=(
            "Outline personal API token. Redacted in all output this script prints. "
            "Consider using a token created solely for this verification run and "
            "revoking it afterward."
        ),
    )
    parser.add_argument(
        "--collection-id",
        required=True,
        help=(
            "ID of a SCRATCH/throwaway Outline collection. This script WILL create a "
            "real document there to inspect the response shape. Never pass the id of a "
            "collection that matters -- create a disposable one for this run."
        ),
    )
    parser.add_argument(
        "--oauth-client-id",
        default=None,
        help="Optional: an OAuth application's client id, to also probe /oauth/authorize.",
    )
    parser.add_argument(
        "--oauth-redirect-uri",
        default=None,
        help="Optional: that application's registered redirect URI.",
    )
    parser.add_argument(
        "--oauth-client-secret",
        default=None,
        help=(
            "Optional: that application's client secret. Redacted in all output. "
            "Combine with --oauth-code to run the full token exchange."
        ),
    )
    parser.add_argument(
        "--oauth-code",
        default=None,
        help=(
            "Optional: the 'code' query parameter from the redirect after approving "
            "the authorize URL this script prints, in a browser, by hand. Required to "
            "check the token exchange and identity-via-OAuth-token assumptions; there "
            "is no way to obtain it without that manual step."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_url = args.base_url.rstrip("/")

    redact = Redactor()
    redact.add(args.token)

    with httpx.Client(base_url=base_url, timeout=args.timeout) as client:
        findings: list[Finding] = []

        create_findings, document_id, document_url = check_create_document(
            client, redact, token=args.token, collection_id=args.collection_id
        )
        findings.extend(create_findings)
        if document_id:
            print(f"\ncreated scratch document id={document_id} url={document_url}")
            print("(not deleted automatically -- clean up the scratch collection if you care to)")

        findings.extend(check_invalid_token(client, redact, collection_id=args.collection_id))
        findings.extend(check_missing_collection(client, redact, token=args.token))

        identity_findings, _ = check_identity_endpoint(client, redact, token=args.token)
        findings.extend(identity_findings)

        findings.extend(check_oauth_authorize_path(redact, base_url=base_url, args=args))
        findings.extend(check_oauth_token_exchange(client, redact, base_url=base_url, args=args))

    return _print_report(findings, redact)


if __name__ == "__main__":
    sys.exit(main())
