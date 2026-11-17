"""P1-3: intermittent EGR deviations must not cancel out in a signed mean."""

from app.diagnostics.engine import SessionData
from app.diagnostics.rules.diesel_bmw import (
    EGR_ERR,
    air_path_evidence,
    egr_error_evidence,
    summarize_egr_error,
)
from app.models.domain import Annotation

CAPS = {"primary_ecu": "7E8", "ecus": [{"ecu": "7E8", "mode01_pids": [0x0C, 0x2D]}]}


def _sd(points, annotations=()):
    samples = [
        dict(mode=1, pid=EGR_ERR, ecu="7E8", quality="ok", value=v, t_mono=100.0 + t) for t, v in points
    ]
    anns = [Annotation(t_wall=0, t_mono=100.0 + t, label=label) for t, label in annotations]
    return SessionData(
        session={"id": "x", "meta": {"capabilities": CAPS}}, samples=samples, scans=[], annotations=anns
    )


def _alternating(amplitude=30.0, hz=2.0, seconds=60.0):
    n = int(seconds * hz)
    return [(i / hz, amplitude if i % 2 == 0 else -amplitude) for i in range(n)]


def test_alternating_plus_minus_30_is_flagged_not_cancelled():
    pts = _alternating()
    st = summarize_egr_error(pts)
    assert abs(st["signed_mean"]) < 1 and st["mean_abs"] == 30.0 and st["max_abs"] == 30.0
    assert st["windows"] and all(w["mean_abs"] == 30.0 for w in st["windows"])
    ev, supports, limits = egr_error_evidence(_sd(pts))
    assert supports and "cancel" in ev.text and "intermittent" in ev.text
    assert ev.values["mean_abs"] == 30.0 and ev.values["signed_mean"] == st["signed_mean"]
    sup, con, *_ = air_path_evidence(_sd(pts))
    assert ev.text in [e.text for e in sup] and not any("EGR" in e.text for e in con)


def test_sustained_offset_is_reported_as_sustained():
    ev, supports, _ = egr_error_evidence(_sd([(i * 0.5, 25.0) for i in range(80)]))
    assert supports and "sustained" in ev.text


def test_small_noise_is_contradicting_evidence():
    pts = [(i * 0.5, (2.0 if i % 2 else -2.0)) for i in range(80)]
    ev, supports, limits = egr_error_evidence(_sd(pts))
    assert ev is not None and not supports and not limits


def test_burst_in_one_window_is_found():
    pts = [(i * 0.5, 40.0 if 20 <= i * 0.5 < 25 else 1.0) for i in range(120)]
    st = summarize_egr_error(pts)
    assert st["mean_abs"] < 15 and [w["t_start"] for w in st["windows"]] == [20.0]
    assert egr_error_evidence(_sd(pts))[1] is True


def test_event_aligned_window_around_annotation():
    pts = [(i * 0.5, 35.0 if 28 <= i * 0.5 <= 32 else 0.5) for i in range(120)]
    ev, supports, _ = egr_error_evidence(_sd(pts, [(30.0, "shudder")]))
    assert supports and "'shudder'" in ev.text
    assert ev.values["events"][0]["label"] == "shudder" and ev.values["events"][0]["mean_abs"] >= 15


def test_sparse_samples_are_a_stated_limitation():
    pts = [(i * 5.0, 30.0 if i % 2 == 0 else -30.0) for i in range(25)]  # 0.2 Hz
    ev, supports, limits = egr_error_evidence(_sd(pts))
    assert ev is not None and any("0.20 Hz" in x and "missed" in x for x in limits)
    # windows need >=3 samples: none at this rate, but mean |error| still exposes the deviation
    assert ev.values["windows"] == [] and supports


def test_too_few_samples_or_too_short_says_nothing():
    for pts in ([(i * 0.5, 30.0) for i in range(10)], [(i * 0.1, 30.0) for i in range(50)]):  # 10 pts / 4.9 s
        ev, supports, limits = egr_error_evidence(_sd(pts))
        assert ev is None and supports is False and "needed" in limits[0]


def test_event_without_enough_samples_is_not_evaluated():
    pts = [(i * 4.0, 1.0) for i in range(30)]
    _, _, limits = egr_error_evidence(_sd(pts, [(10.0, "knock")]))
    assert any("'knock'" in x and "not evaluated" in x for x in limits)
