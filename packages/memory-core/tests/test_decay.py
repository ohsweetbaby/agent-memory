import math
from datetime import datetime, timedelta, timezone

import pytest

from memory_core.decay import (
    DecayParams,
    apply_rehearsal,
    retention,
    should_archive,
    stability_days,
)

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def test_retention_is_one_at_zero_elapsed():
    assert retention(NOW, NOW, 10.0) == 1.0


def test_higher_importance_decays_slower():
    assert stability_days(0.9, 0, 1.0, DecayParams()) > stability_days(0.2, 0, 1.0, DecayParams())


def test_30_days_low_importance_is_archived_high_is_not():
    low = NOW - timedelta(days=30)
    assert should_archive(low, NOW, 0.2, 0, 1.0, DecayParams()) is True
    assert should_archive(low, NOW, 0.9, 3, 1.5, DecayParams()) is False


def test_rehearsal_doubles_stability_capped():
    assert apply_rehearsal(1.0, DecayParams()) == 1.5
    assert apply_rehearsal(9.0, DecayParams()) == 10.0


def test_archive_threshold_boundary_is_strict():
    # S=7 天、Δ=7 天时 R=exp(-1)，R 等于阈值不归档
    params = DecayParams(archive_threshold=math.exp(-1))
    assert should_archive(NOW - timedelta(days=7), NOW, 0.0, 0, 0.0, params) is False
    assert should_archive(NOW - timedelta(days=7.01), NOW, 0.0, 0, 0.0, params) is True


def test_params_defaults_match_spec():
    params = DecayParams()
    assert (
        params.base_stability_days,
        params.archive_threshold,
        params.importance_alpha,
        params.access_beta,
        params.rehearsal_factor,
        params.max_rehearsal_factor,
    ) == (7.0, 0.15, 2.0, 0.8, 1.5, 10.0)


def test_stability_follows_spec_formula():
    params = DecayParams()
    expected = 7.0 * (1 + 2.0 * 0.5) * (1 + 0.8 * math.log(4)) * 2.0
    assert stability_days(0.5, 3, 2.0, params) == pytest.approx(expected)


def test_non_positive_rehearsal_factor_is_neutral():
    params = DecayParams()
    neutral = stability_days(0.4, 2, 1.0, params)
    assert stability_days(0.4, 2, 0.0, params) == neutral
    assert stability_days(0.4, 2, -3.0, params) == neutral


def test_future_timestamp_is_treated_as_zero_elapsed():
    future = NOW + timedelta(days=30)
    assert retention(future, NOW, 5.0) == 1.0
    assert should_archive(future, NOW, 0.0, 0, 1.0, DecayParams()) is False


def test_retention_uses_elapsed_days():
    assert retention(NOW - timedelta(days=7), NOW, 7.0) == pytest.approx(math.exp(-1.0))
