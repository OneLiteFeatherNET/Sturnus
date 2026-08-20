"""Process-wide counters and their Prometheus exposition (Spec 4.1).

The lesson of the decode incident is that the failure was *invisible*: a
recording ended, nobody noticed, and the first evidence was a closed
session row with zero participants. Structured ERROR logs and a message in
the channel are two of the three answers; this is the third, because
"visible rather than silent" should not depend on someone happening to
read logs.

Deliberately tiny and dependency-free -- Prometheus text exposition is a
documented plain-text format, and adding a client library to emit half a
dozen counters would be the wrong trade. Counters only: every quantity
here is monotonic, and a gauge or histogram would need a semantics
discussion this does not.

`COUNTERS` is a process-wide default so `/metrics` and the voice adapter
find the same instance without threading one through four constructors,
but every consumer takes it as a parameter, so a test injects its own and
asserts on it without touching global state.
"""

from __future__ import annotations

import threading

#: Frames that decoded cleanly. Incremented once, by
#: `ResilientOpusDecoder`, which is the single point every frame from a
#: consenting speaker passes through.
FRAMES_DECODED = "sturnus_voice_frames_decoded_total"
#: Frames the decoder could not read, labelled `code` with the libopus
#: error code (`code="-4"` is the production corrupted-stream case) or
#: `code="unknown"` for a failure that carried no code.
FRAMES_DISCARDED = "sturnus_voice_frames_discarded_total"
#: Frames the network lost, which the library reported as a fake packet.
FRAMES_LOST = "sturnus_voice_frames_lost_total"
#: Frames arriving for an SSRC with no member attached yet -- never
#: decoded, never written, because no consent record can be checked for an
#: identity we do not know.
FRAMES_UNATTRIBUTED = "sturnus_voice_frames_unattributed_total"
#: Frames dropped because this speaker's stored consent record was not
#: cached yet. The verdict is fetched off the drain, so the first frames of
#: a speaker's first utterance can arrive before it is known -- and audio
#: whose consent we cannot vouch for is not recorded.
FRAMES_AWAITING_CONSENT = "sturnus_voice_frames_awaiting_consent_total"
#: Frames dropped because the event loop fell far enough behind that the
#: hand-off queue filled. Should never fire; if it does, this is evidence.
#: Only ever audio: control messages are not subject to this bound.
QUEUE_DROPPED = "sturnus_voice_queue_dropped_total"
#: Per-stream escalations, labelled by the state that was reached.
STREAM_STATE_CHANGES = "sturnus_voice_stream_state_changes_total"
#: Sessions closed because *every* stream stopped decoding.
DECODE_TOTAL_FAILURES = "sturnus_voice_decode_total_failures_total"
#: The library's `after=` hook firing, i.e. capture stopped on its own.
CAPTURE_STOPPED = "sturnus_voice_capture_stopped_total"
#: Anything that escaped the sink's own logic and hit its outer guard.
SINK_ERRORS = "sturnus_voice_sink_errors_total"

_Key = tuple[str, tuple[tuple[str, str], ...]]


class Counters:
    """A flat map of monotonic counters, safe to increment from any thread.

    Incremented from the packet-router thread and read from the event loop
    serving `/metrics`, so the lock is not optional. It is uncontended: an
    increment is a dictionary update.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[_Key, float] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def get(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            return self._values.get(key, 0.0)

    def snapshot(self) -> dict[_Key, float]:
        with self._lock:
            return dict(self._values)


def render_prometheus(snapshot: dict[_Key, float]) -> str:
    """Renders a snapshot as Prometheus text exposition.

    One `# TYPE` line per metric name, then one sample per label set, in a
    stable order so a diff between two scrapes is readable by a human.
    """
    lines: list[str] = []
    by_name: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = {}
    for (name, labels), value in snapshot.items():
        by_name.setdefault(name, []).append((labels, value))

    for name in sorted(by_name):
        lines.append(f"# TYPE {name} counter")
        for labels, value in sorted(by_name[name]):
            lines.append(f"{name}{_render_labels(labels)} {value:g}")
    return "\n".join(lines) + "\n" if lines else ""


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + rendered + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


#: The default instance every production caller shares.
COUNTERS = Counters()
