from arol_ai.domain.models import TelemetrySnapshot, describe_reading
from arol_ai.graph.state import Evidence


def build_telemetry_evidence(telemetry: TelemetrySnapshot) -> Evidence:
    findings = diagnose_telemetry(telemetry)

    return Evidence(
        source="telemetry",
        title="Latest machine telemetry",
        excerpt=" ".join(findings),
        confidence=_telemetry_confidence(telemetry),
    )


def diagnose_telemetry(telemetry: TelemetrySnapshot) -> list[str]:
    findings = [
        (
            f"Health {telemetry.health}; {describe_reading(telemetry)}; "
            f"active alarm {telemetry.active_alarm}."
        )
    ]

    if telemetry.source:
        findings.append(f"Telemetry source is {telemetry.source}.")

    if telemetry.quality == "stale":
        findings.append(
            "Telemetry is stale and should be treated as historical context until a fresh "
            "snapshot is available."
        )
    elif telemetry.quality == "partial":
        missing = ", ".join(telemetry.missing_fields)
        findings.append(f"Telemetry is partial; missing fields: {missing}.")
    elif telemetry.quality in {"invalid", "invalid-timestamp"}:
        findings.append(
            "Telemetry quality is invalid, so live machine state should be confirmed before acting."
        )
    elif telemetry.quality:
        findings.append(f"Telemetry quality is {telemetry.quality}.")

    if telemetry.age_seconds is not None:
        findings.append(f"Telemetry age is {telemetry.age_seconds} seconds.")

    # Judged against this machine's own rated speed, not a fleet-wide constant.
    # requirements/README.md: two machines of one model can have very different
    # nominal rates, so a bare bottles-per-hour figure decides nothing. The
    # threshold this replaced was a simulator-era torque constant.
    if (
        telemetry.production_rate_bph
        and telemetry.rate_utilization_pct is not None
        and telemetry.rate_utilization_pct < 80
    ):
        findings.append(
            f"Production is at {telemetry.rate_utilization_pct:.0f}% of this machine's nominal "
            f"rate, so the shortfall should be investigated against its own rated speed."
        )

    if telemetry.temperature_c >= 40:
        findings.append(
            "Temperature is elevated enough to include lubrication and friction checks."
        )

    if telemetry.has_active_alarm:
        findings.append(
            "An active alarm is present, so operator checks should be followed by technician "
            "escalation if it persists."
        )

    return findings


def _telemetry_confidence(telemetry: TelemetrySnapshot) -> float:
    if telemetry.quality == "fresh":
        return 0.88

    if telemetry.quality == "stale":
        return 0.55

    if telemetry.quality == "partial":
        return 0.48

    if telemetry.quality in {"invalid", "invalid-timestamp"}:
        return 0.35

    return 0.72
