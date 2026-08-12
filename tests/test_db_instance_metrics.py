from __future__ import annotations

import pytest

from server.core.db_instance_metrics import (
    DBInstanceMetricSample,
    emit_dedicated_pool_capacity,
    emit_dedicated_pool_signal,
    set_db_instance_metric_sink,
)


@pytest.fixture(autouse=True)
def reset_metric_sink():
    set_db_instance_metric_sink(None)
    yield
    set_db_instance_metric_sink(None)


def test_dedicated_metrics_use_bounded_signal_and_outcome_labels() -> None:
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    emit_dedicated_pool_signal(
        signal="allocation",
        outcome="hot_hit",
        duration_seconds=0.025,
    )

    assert samples == [
        DBInstanceMetricSample(
            name="agentic_db_dedicated_pool_event_total",
            duration_seconds=0.025,
            labels={
                "signal": "allocation",
                "outcome": "hot_hit",
                "backend_type": "dedicated_pool",
            },
            value=1.0,
        )
    ]


@pytest.mark.parametrize(
    ("signal", "outcome"),
    [
        ("pool-secret-name", "hot_hit"),
        ("allocation", "mysql-user@example.com"),
        ("readiness", "raw exception text"),
    ],
)
def test_dedicated_metrics_reject_unbounded_labels(
    signal: str, outcome: str
) -> None:
    with pytest.raises(ValueError):
        emit_dedicated_pool_signal(signal=signal, outcome=outcome)


def test_stale_evidence_does_not_emit_replenishment_purchase() -> None:
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    emit_dedicated_pool_signal(
        signal="readiness", outcome="stale_excluded"
    )

    assert [sample.labels for sample in samples] == [
        {
            "signal": "readiness",
            "outcome": "stale_excluded",
            "backend_type": "dedicated_pool",
        }
    ]
    assert all(
        sample.labels["signal"] != "replenishment"
        for sample in samples
    )


def test_replenishment_purchase_signal_supports_velocity_alerting() -> None:
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    emit_dedicated_pool_signal(
        signal="replenishment", outcome="purchase_reserved"
    )

    assert samples[0].labels == {
        "signal": "replenishment",
        "outcome": "purchase_reserved",
        "backend_type": "dedicated_pool",
    }


def test_capacity_gauges_use_bounded_kinds() -> None:
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    emit_dedicated_pool_capacity(kind="billable_total", value=7)

    assert samples[0].name == "agentic_db_dedicated_pool_capacity"
    assert samples[0].value == 7
    assert samples[0].labels == {
        "kind": "billable_total",
        "backend_type": "dedicated_pool",
    }
    with pytest.raises(ValueError):
        emit_dedicated_pool_capacity(kind="customer-pool", value=1)
