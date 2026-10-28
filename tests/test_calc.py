"""The pure rebase of the sensor's statistics."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.novafos_watts.calc import rebase_entity_statistics

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)
M5 = timedelta(minutes=5)


def changes(rows):
    return [b[1] - a[1] for a, b in zip(rows, rows[1:])]


def test_without_short_term_rows_nothing_to_join():
    assert rebase_entity_statistics([(T0, 1.0)], [T0], []) is None


def test_recorder_series_moves_onto_the_true_total():
    # Imported hours 0-4 (cumulative 1..5), then the recorder started at hour 5
    # with sum 0 while the sensor showed 5.0: the bug.
    true = [(T0 + i * H, float(i + 1)) for i in range(5)]
    short = [(T0 + 5 * H + i * M5, 5.0 + 0.1 * (i // 12), 0.1 * (i // 12)) for i in range(24)]
    plan = rebase_entity_statistics(true, [T0 + i * H for i in range(7)], short)

    assert plan.frame_start == T0 + 5 * H
    assert plan.offset == pytest.approx(5.0)
    assert plan.rows == [(T0 + i * H, float(i + 1)) for i in range(5)] + [
        (T0 + 5 * H, 5.0),
        (T0 + 6 * H, pytest.approx(5.1)),
    ]


def test_hours_learned_after_the_recorder_started_are_capped():
    # The sensor said 3.0 when the recorder started at hour 5; hours 3-4 were
    # only learned later. Taking their true sums (4, 5) would drop back to 3.0.
    true = [(T0 + i * H, float(i + 1)) for i in range(5)]
    short = [(T0 + 5 * H, 3.0, 0.0), (T0 + 6 * H, 5.0, 2.0)]
    plan = rebase_entity_statistics(true, [], short)

    assert [v for _, v in plan.rows] == [1.0, 2.0, 3.0, 3.0, 3.0, 3.0, 5.0]
    assert min(changes(plan.rows)) >= 0


def test_only_the_newest_series_counts_and_gaps_carry_over():
    # An older series (offset 2) before the current one (offset 7); hour 2 has
    # a row but no true value.
    true = [(T0, 1.0), (T0 + H, 2.0), (T0 + 3 * H, 4.0)]
    short = [(T0 + 4 * H, 4.0, 2.0), (T0 + 5 * H, 7.0, 0.0), (T0 + 6 * H, 8.0, 1.0)]
    plan = rebase_entity_statistics(true, [T0 + 2 * H], short)

    assert plan.frame_start == T0 + 5 * H
    assert plan.offset == 7.0
    assert plan.rows == [
        (T0, 1.0),
        (T0 + H, 2.0),
        (T0 + 2 * H, 2.0),
        (T0 + 3 * H, 4.0),
        (T0 + 5 * H, 7.0),
        (T0 + 6 * H, 8.0),
    ]
