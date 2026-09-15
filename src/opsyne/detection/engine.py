"""Deterministic investigation candidates; findings do not authorize operations."""

from __future__ import annotations

import hashlib

from opsyne.contracts.observations import CommonEvent, Coverage, Finding, Severity


def detect(event: CommonEvent) -> list[Finding]:
    findings: list[Finding] = []

    def add(kind: str, title: str, severity: Severity) -> None:
        key = hashlib.sha256(f"{event.raw_ref}:{kind}".encode()).hexdigest()
        findings.append(
            Finding(
                dedup_key=key,
                severity=severity,
                title=title,
                kind=kind,
                evidence_ids=[event.raw_ref],
                service_id=event.service_id,
                source_id=event.source_id,
            )
        )

    if event.parse_status != "KNOWN":
        add("interpretation", f"Observation interpretation is {event.parse_status}", "WARNING")
    if event.outcome == "FAILURE" or event.severity in ("ERROR", "CRITICAL"):
        severity: Severity = "CRITICAL" if event.severity == "CRITICAL" else "ERROR"
        add("operation_failure", "Observed failure requires investigation", severity)
    if event.category and event.category.lower() in {"security", "authentication", "authorization"}:
        add("security", "Security-related observation requires review", "WARNING")
    return findings


def detect_coverage(coverage: Coverage) -> list[Finding]:
    if coverage.status in ("healthy", "disabled") and coverage.gap_count == 0:
        return []
    # A continuing outage keeps one candidate; new gap generations remain distinct.
    identity = f"coverage:{coverage.source_id}:{coverage.status}:{coverage.gap_count}"
    return [
        Finding(
            dedup_key=hashlib.sha256(identity.encode()).hexdigest(),
            severity="WARNING",
            title=f"Observation coverage: {coverage.status}; gaps={coverage.gap_count}",
            kind="coverage",
            evidence_ids=[],
            service_id=coverage.service_id,
            source_id=coverage.source_id,
        )
    ]
