from arol_ai.graph.state import Evidence
from arol_ai.rag.evidence_quality import sanitize_manual_evidence

TEACHING_FOOTER = (
    "AROL S.p.A. - teaching copy, Politecnico di Torino, "
    "System and Device Programming. Do not redistribute."
)


def _manual(title: str, excerpt: str) -> Evidence:
    return Evidence(source="manual", title=title, section=title, excerpt=excerpt)


def test_drops_teaching_footer_and_training_form_sources() -> None:
    evidence = [
        _manual("Manual", TEACHING_FOOTER),
        _manual(
            "Report of the training",
            "Trainer/s signature/s: Mr. ______\nNAME | CHARGE | SIGNATURE\n" + TEACHING_FOOTER,
        ),
    ]

    assert sanitize_manual_evidence(evidence) == []


def test_retains_technical_passage_without_course_footer() -> None:
    evidence = _manual(
        "Torque check",
        "Verify the guarded torque setting before restarting the machine.\n" + TEACHING_FOOTER,
    )

    sanitized = sanitize_manual_evidence([evidence])

    assert len(sanitized) == 1
    assert sanitized[0].excerpt == (
        "Verify the guarded torque setting before restarting the machine."
    )


def test_drops_educational_notice_and_table_of_contents() -> None:
    evidence = [
        _manual(
            "NOTICE - EDUCATIONAL USE ONLY",
            "This document is for teaching purposes only. NO OPERATIONAL RELIANCE.",
        ),
        _manual(
            "Safety norms",
            "USE OF THE TURRET . . . . . . . . . . . . . . . . . . . . 5",
        ),
    ]

    assert sanitize_manual_evidence(evidence) == []


def test_keeps_non_manual_evidence_unchanged() -> None:
    evidence = Evidence(
        source="telemetry",
        title="AL031 - Heads motor overload",
        excerpt="AL031 has four recorded occurrences and one is unresolved.",
    )

    assert sanitize_manual_evidence([evidence]) == [evidence]
