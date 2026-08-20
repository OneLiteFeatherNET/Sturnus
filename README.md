# Sturnus

Discord voice transcription with Outline document output.

Sturnus joins one dedicated voice channel per guild, records each
participant's audio into a separate stream for the duration of the
session, and — once the session ends — transcribes each stream with
faster-whisper and assembles the results into a single chronological
protocol, posted as a document in Outline and linked back into the
channel's text chat.

Nobody may be recorded without consent: the recording channel denies
`Speak` to `@everyone` and allows it only for a role granted through
`/consent`, and the bot additionally drops any audio packet from a user
who does not hold that role, even if Discord permissions alone would have
let it through (guild administrators bypass channel overwrites). See
[docs/operations.md](docs/operations.md) for why a non-recorded channel
must also exist alongside it.

Recordings are encrypted before they ever leave the pod and are kept only
for a configured retention window to allow reprocessing a poor
transcription; see [docs/operations.md](docs/operations.md) for what that
means operationally, including what happens if the encryption key is
lost.

## Running locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

The bot needs a PostgreSQL database and an S3-compatible object store
reachable before it can start; point it at them (and everything else it
needs) through environment variables — see
[docs/operations.md](docs/operations.md) for the full list. With those
set:

```bash
uv run sturnus-bot     # Discord gateway connection + recording
uv run sturnus-worker  # transcription + document publishing
uv run sturnus-link    # OAuth account-link callback
```

Run the checks the same way CI does:

```bash
uv run pytest
uv run mypy
uv run ruff check
```

## License

Sturnus is licensed under the GNU Affero General Public License v3.0 or
later (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text. The
AGPL was chosen deliberately because Sturnus is a self-hosted network
service: anyone interacting with a modified deployment over Discord is
entitled, under section 13 of the license, to receive that deployment's
corresponding source. The bot's `/about` command exists to make that
offer directly to users.

## Documentation

- [docs/operations.md](docs/operations.md) — environment variables, the
  master key and what its loss means, Discord setup, first run,
  troubleshooting, retention.
- [docs/superpowers/specs/2026-08-19-sturnus-design.md](docs/superpowers/specs/2026-08-19-sturnus-design.md)
  — full design document.
