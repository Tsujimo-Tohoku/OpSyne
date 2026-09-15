"""Observation durability and fail-closed interpretation acceptance tests."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from opsyne.collector.service import Collector, DuplicateConflict
from opsyne.contracts.observations import AdapterDefinition, RawEvent, RawInput, Source
from opsyne.detection.engine import detect, detect_coverage
from opsyne.normalization.engine import normalize


def source(**values: object) -> Source:
    return Source.model_validate(
        {
            "id": "audit",
            "service_id": "svc",
            "name": "Audit",
            "kind": "push",
            **values,
        }
    )


def adapter(**values: object) -> AdapterDefinition:
    return AdapterDefinition.model_validate(
        {
            "id": "audit-v1",
            "name": "Audit JSON",
            "source_id": "audit",
            "target_instance_id": "instance-1",
            "target_version": 1,
            "version": 1,
            "fields": {"outcome": "result", "severity": "level", "message": "message"},
            "outcome_map": {"ok": "SUCCESS", "denied": "FAILURE"},
            "severity_map": {"info": "INFO", "error": "ERROR"},
            **values,
        }
    )


def raw(collector: Collector, payload: str, external_id: str = "1") -> RawEvent:
    return collector.ingest("audit", [RawInput(external_id=external_id, payload=payload)], now=100)[
        0
    ]


def test_receipts_survive_restart_and_batch_conflict_rolls_back(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    collector = Collector(database)
    collector.register_source(source())
    event = raw(collector, '{"x":1}')
    restarted = Collector(database)
    assert restarted.pending() == [event]
    assert raw(restarted, '{"x":1}') == event
    assert (
        restarted.ingest(
            "audit",
            [
                RawInput(external_id="1", payload='{"x":1}', observed_at=99),
            ],
        )[0]
        == event
    )
    with pytest.raises(DuplicateConflict):
        restarted.ingest(
            "audit",
            [
                RawInput(external_id="2", payload="new"),
                RawInput(external_id="1", payload="changed"),
            ],
            now=101,
        )
    assert restarted.search() == [event]
    assert restarted.coverage(now=101)[0].pending_count == 1
    restarted.ack(event.id, "INVALID")
    again = Collector(database)
    assert again.pending() == []
    assert again.raw(event.id) == event
    coverage = again.coverage(now=500)[0]
    assert coverage.status == "stale"
    assert coverage.parse_failures == 1
    assert coverage.unknown_count == 0
    with again.db.connection() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE observation_raw SET payload='changed' WHERE id=?", (event.id,))


def test_coverage_distinguishes_missing_unknown_invalid_and_disabled(tmp_path: Path) -> None:
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source())
    assert collector.coverage(now=100)[0].status == "missing"
    first = raw(collector, "{}")
    second = raw(collector, "bad", external_id="2")
    collector.ack(first.id, "UNKNOWN")
    collector.ack(second.id, "INVALID")
    current = collector.coverage(now=101)[0]
    assert current.status == "healthy"
    assert current.unknown_count == current.parse_failures == 1
    collector.register_source(source(enabled=False))
    assert collector.coverage(now=1000)[0].status == "disabled"
    with pytest.raises(ValueError, match="disabled"):
        raw(collector, "{}")
    with pytest.raises(ValueError, match="immutable"):
        collector.register_source(source(service_id="another"))


def test_normalizer_requires_exact_current_binding_and_one_adapter(tmp_path: Path) -> None:
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source())
    event = raw(collector, '{"result":"denied","level":"error","message":"failed"}')
    definition = adapter()
    known = normalize(event, [definition], target_instance_id="instance-1", target_version=1)
    assert known.parse_status == "KNOWN"
    assert known.outcome == "FAILURE"
    assert known.raw_ref == event.id
    assert known.adapter_version == 1
    assert known.timestamp is None
    assert normalize(event, [definition]).parse_status == "UNKNOWN"
    assert (
        normalize(
            event,
            [definition],
            target_instance_id="replacement",
            target_version=1,
        ).parse_status
        == "UNKNOWN"
    )
    assert (
        normalize(
            event,
            [definition],
            target_instance_id="instance-1",
            target_version=2,
        ).parse_status
        == "UNKNOWN"
    )
    assert (
        normalize(
            event,
            [definition, adapter(id="competing")],
            target_instance_id="instance-1",
            target_version=1,
        ).parse_status
        == "UNKNOWN"
    )
    assert detect(known) == detect(known)
    assert detect(known)[0].evidence_ids == [event.id]


def test_partial_unknown_malformed_and_type_change_preserve_uncertainty(tmp_path: Path) -> None:
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source())
    definition = adapter()
    partial = normalize(
        raw(collector, '{"result":"custom","level":"info","message":99}'),
        [definition],
        target_instance_id="instance-1",
        target_version=1,
    )
    assert partial.parse_status == "PARTIAL"
    assert partial.outcome == "UNKNOWN"
    assert partial.message is None
    assert set(partial.unknown_fields) == {"outcome", "message"}
    for payload, expected in [
        ("{", "INVALID"),
        ('{"bad":NaN}', "INVALID"),
        ('{"bad":1e999}', "INVALID"),
        ("[]", "UNKNOWN"),
        ("{}", "UNKNOWN"),
    ]:
        event = raw(collector, payload, external_id=payload)
        result = normalize(event, [definition], target_instance_id="instance-1", target_version=1)
        assert result.parse_status == expected
        assert result.outcome == "UNKNOWN"
        assert detect(result)[0].kind == "interpretation"


def test_adapter_rejects_code_and_condition_type_confusion(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        adapter(code="import os")
    with pytest.raises(ValidationError):
        adapter(fields={"message": "eval('x')"})
    with pytest.raises(ValidationError):
        adapter(outcome_map={"ok": "approved"})
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source())
    event = raw(collector, '{"format":true,"result":"ok","level":"info","message":"hello"}')
    conditional = adapter(conditions={"format": 1})
    assert (
        normalize(
            event,
            [conditional],
            target_instance_id="instance-1",
            target_version=1,
        ).parse_status
        == "UNKNOWN"
    )


def test_file_partial_line_restart_rotation_and_original_bytes(tmp_path: Path) -> None:
    logfile = tmp_path / "audit.log"
    logfile.write_bytes(b'{"x":1}\n{"part":')
    database = tmp_path / "state.sqlite3"
    collector = Collector(database)
    collector.register_source(source(kind="file", path=str(logfile)))
    first = collector.poll_files(now=100)
    assert len(first) == 1
    assert first[0].payload == '{"x":1}\n'
    assert collector.coverage(now=100)[0].cursor == 8
    collector = Collector(database)
    assert collector.poll_files(now=101) == []
    with logfile.open("ab") as stream:
        stream.write(b"2}\n\xff\n")
    more = collector.poll_files(now=102)
    assert len(more) == 2
    assert more[0].payload == '{"part":2}\n'
    assert base64.b64decode(more[1].raw_bytes_b64) == b"\xff\n"
    assert normalize(more[1], []).parse_status == "INVALID"
    logfile.rename(tmp_path / "audit.old")
    logfile.write_bytes(b'{"x":1}\n')
    rotated = collector.poll_files(now=103)
    assert len(rotated) == 1
    assert rotated[0].id != first[0].id
    assert collector.raw(first[0].id) == first[0]
    coverage = collector.coverage(now=103)[0]
    assert coverage.gap_count == 1
    assert coverage.last_gap_at == 103
    assert detect_coverage(coverage)[0].kind == "coverage"
    assert collector.poll_files(now=104) == []


def test_truncated_and_rewritten_file_is_a_new_generation(tmp_path: Path) -> None:
    logfile = tmp_path / "audit.log"
    logfile.write_bytes(b"long original entry\n")
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source(kind="file", path=str(logfile)))
    first = collector.poll_files(now=100)[0]
    logfile.write_bytes(b"new\n")
    second = collector.poll_files(now=101)[0]
    assert first.external_id != second.external_id
    assert collector.coverage(now=101)[0].gap_count == 1
    # Same inode and larger replacement is detected by the cursor anchor as well.
    logfile.write_bytes(b"replacement considerably longer\n")
    assert len(collector.poll_files(now=102)) == 1
    assert collector.coverage(now=102)[0].gap_count == 2
    assert len(collector.search()) == 3
    logfile.unlink()
    assert collector.poll_files(now=103) == []
    assert collector.coverage(now=103)[0].status == "missing"


def test_limits_and_unregistered_sources_are_rejected(tmp_path: Path) -> None:
    collector = Collector(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="unknown"):
        collector.ingest("missing", [])
    with pytest.raises(ValueError, match="limit"):
        collector.pending(1001)
    with pytest.raises(ValueError, match="query"):
        collector.search(query="x" * 1001)
    with pytest.raises(ValueError, match="unknown"):
        collector.ack("missing")


def test_file_originals_and_cursor_roll_back_together_on_storage_failure(tmp_path: Path) -> None:
    logfile = tmp_path / "audit.log"
    logfile.write_bytes(b"first\nsecond\n")
    database = tmp_path / "state.sqlite3"
    collector = Collector(database)
    collector.register_source(source(kind="file", path=str(logfile)))
    with collector.db.connection() as connection:
        connection.execute("""
            CREATE TRIGGER simulate_disk_failure BEFORE INSERT ON observation_raw
            WHEN NEW.payload LIKE 'second%'
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="simulated storage failure"):
        collector.poll_files(now=100)
    restarted = Collector(database)
    assert restarted.pending() == []
    assert restarted.coverage(now=100)[0].cursor == 0
    with restarted.db.connection() as connection:
        connection.execute("DROP TRIGGER simulate_disk_failure")
    assert len(restarted.poll_files(now=101)) == 2
    assert restarted.coverage(now=101)[0].cursor == len(logfile.read_bytes())


def test_timestamp_requires_explicit_timezone_and_missing_fields_are_not_facts(
    tmp_path: Path,
) -> None:
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source())
    definition = adapter(fields={"timestamp": "time", "outcome": "result", "severity": "level"})
    event = raw(collector, '{"time":"2026-09-15T01:00:00","result":"ok","level":"info"}')
    result = normalize(event, [definition], target_instance_id="instance-1", target_version=1)
    assert result.parse_status == "PARTIAL"
    assert result.timestamp is None
    assert result.actor is None
    explicit = raw(collector, '{"time":"2026-09-15T01:00:00Z","result":"ok","level":"info"}', "2")
    result = normalize(explicit, [definition], target_instance_id="instance-1", target_version=1)
    assert result.parse_status == "KNOWN"
    assert result.timestamp is not None


def test_retained_iteration_pages_beyond_search_limit_without_holding_write_lock(
    tmp_path: Path,
) -> None:
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source())
    for start in (0, 500, 1000):
        collector.ingest(
            "audit",
            [
                RawInput(external_id=str(index), payload="{}")
                for index in range(start, min(start + 500, 1001))
            ],
        )
    iterator = collector.iter_raw(source_id="audit")
    first = next(iterator)
    # These writes prove the iterator closed its database transaction before yielding.
    collector.ack(first.id, "UNKNOWN")
    collector.ingest("audit", [RawInput(external_id="late-arrival", payload="{}")])
    retained = [first, *iterator]
    assert len(retained) == 1001
    assert len({item.id for item in retained}) == 1001
    assert all(item.external_id != "late-arrival" for item in retained)
    assert len(list(collector.iter_raw())) == 1002
    assert list(collector.iter_raw("absent-source")) == []


def test_reinterpretation_updates_coverage_without_acknowledging_live_delivery(
    tmp_path: Path,
) -> None:
    collector = Collector(tmp_path / "state.sqlite3")
    collector.register_source(source())
    waiting = raw(collector, "{}", "waiting")
    delivered = raw(collector, "{}", "delivered")
    collector.ack(delivered.id, "UNKNOWN")
    collector.record_parse_status(waiting.id, "PARTIAL")
    collector.record_parse_status(delivered.id, "KNOWN")
    restarted = Collector(tmp_path / "state.sqlite3")
    assert restarted.pending() == [waiting]
    coverage = restarted.coverage(now=101)[0]
    assert coverage.pending_count == 1
    assert coverage.unknown_count == 1
    restarted.ack(waiting.id, "KNOWN")
    assert restarted.pending() == []
    with pytest.raises(ValueError, match="unknown"):
        restarted.record_parse_status("absent", "KNOWN")
