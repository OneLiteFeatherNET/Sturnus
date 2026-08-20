"""Counter arithmetic and Prometheus exposition.

Small, but not decoration: these counters are the third answer to the
incident, after structured logs and the message in the channel. They are
also what turns the deferred FEC decision from an argument into a
measurement -- `frames_lost / frames_seen` on a real session is the named
trigger for implementing it.
"""

from __future__ import annotations

import threading

from sturnus.infrastructure.metrics import Counters, render_prometheus


def test_counters_start_at_zero_rather_than_missing() -> None:
    """A metric that has not fired yet is zero, not absent."""
    assert Counters().get("sturnus_voice_frames_decoded_total") == 0.0


def test_label_sets_are_counted_separately() -> None:
    counters = Counters()

    counters.inc("frames", code="-4")
    counters.inc("frames", code="-4")
    counters.inc("frames", code="-1")

    assert counters.get("frames", code="-4") == 2.0
    assert counters.get("frames", code="-1") == 1.0
    assert counters.get("frames") == 0.0


def test_labels_are_order_independent() -> None:
    counters = Counters()

    counters.inc("frames", a="1", b="2")

    assert counters.get("frames", b="2", a="1") == 1.0


def test_increments_from_several_threads_are_not_lost() -> None:
    """Incremented from the packet-router thread, read from the event loop."""
    counters = Counters()

    def bump() -> None:
        for _ in range(1000):
            counters.inc("frames")

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counters.get("frames") == 4000.0


def test_rendering_groups_by_name_and_is_stably_ordered() -> None:
    counters = Counters()
    counters.inc("b_total", 2.0)
    counters.inc("a_total", 1.0, state="degraded")
    counters.inc("a_total", 3.0, state="unusable")

    assert render_prometheus(counters.snapshot()) == (
        "# TYPE a_total counter\n"
        'a_total{state="degraded"} 1\n'
        'a_total{state="unusable"} 3\n'
        "# TYPE b_total counter\n"
        "b_total 2\n"
    )


def test_an_empty_registry_renders_a_valid_empty_exposition() -> None:
    assert render_prometheus(Counters().snapshot()) == ""


def test_label_values_are_escaped() -> None:
    """A display name or an error message must not be able to forge a sample."""
    counters = Counters()
    counters.inc("frames", reason='he said "no"\nfake_total 99')

    rendered = render_prometheus(counters.snapshot())

    assert rendered == (
        '# TYPE frames counter\nframes{reason="he said \\"no\\"\\nfake_total 99"} 1\n'
    )
