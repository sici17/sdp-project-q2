import pytest

from arol_ai.graph.answer_composer import StructuredAnswerComposer
from arol_ai.graph.nodes import _manual_guidance_sentence, answer_node
from arol_ai.graph.state import Evidence, GraphState

MANUAL_SUMMARY_PREFIX = "Manual summary\n- "
CITATION_HEADING = "\nCitation\n-"


#: Passages taken verbatim from manuals in the fleet, used to check that an
#: answer is built from the passage it cites rather than from anything written
#: into the code.
GOLDEN_PASSAGES = [
    pytest.param(
        "Walk me through the verification of correct phase sequence",
        Evidence(
            source="manual",
            title="VERIFICATION OF CORRECT PHASE SEQUENCE",
            excerpt=(
                "9.1.2 VERIFICATION OF CORRECT PHASE SEQUENCE After carrying out the "
                "electric connections, operate as follows: rotate the switch to MAN. "
                "Insert the DOOR LOCK disconnecting knife switch. Check that the "
                "EMERGENCY button is not pressed. Press RESET. Press the control to lift "
                "the closure carousel. Verify that the closure carousel raises."
            ),
            page=62,
            score=0.91,
        ),
        ("carousel", "reset"),
        id="phase-sequence-procedure",
    ),
    pytest.param(
        "What does AL017_LOW_AIR_PRESSURE mean?",
        Evidence(
            source="manual",
            title="MESSAGE DESCRIPTION RESET MODE",
            excerpt=(
                "MESSAGE DESCRIPTION RESET MODE LOW AIR PRESSURE (SP1) A pressure lack in "
                "the compressed air circuit does not enable the closing machine correct "
                "operation. Check the compressed air supply line."
            ),
            page=83,
            score=0.99,
            chunk_kind="troubleshooting",
        ),
        ("pressure", "air"),
        id="dataset-alarm-code",
    ),
    pytest.param(
        "How do I adjust the closure gripper pressure on the cap?",
        Evidence(
            source="manual",
            title="CLOSURE GRIPPER ADJUSTMENTS",
            excerpt=(
                "10.4.3 GRIP PRESSURE OF GRIPPER ON THE CAP Remove the closure gripper "
                "from the closure head, then proceed as follows: loosen the grub screws "
                "of the spring adjustment threaded ring."
            ),
            page=118,
            score=0.94,
        ),
        ("gripper", "spring"),
        id="gripper-pressure-adjustment",
    ),
]


@pytest.mark.parametrize(("query", "evidence", "expected_terms"), GOLDEN_PASSAGES)
def test_manual_answer_is_built_from_the_passage_it_cites(
    query: str,
    evidence: Evidence,
    expected_terms: tuple[str, ...],
) -> None:
    """An answer must be grounded in, and traceable to, its citation.

    The guidance layer once carried prose written against the section titles of
    one legacy manual. It read well, but it was not taken from the manual, so
    with a different corpus it attached confident instructions to whatever page
    happened to be cited. What is asserted here instead is the property that
    actually matters: the wording comes from the cited passage, and the citation
    is present and points at a page.
    """
    guidance = _manual_guidance_sentence(evidence, query)

    assert guidance.startswith(MANUAL_SUMMARY_PREFIX)
    assert CITATION_HEADING in guidance
    assert f"page {evidence.page}" in guidance

    # Every substantive word of the answer has to be traceable to the passage,
    # its title, or the operator's own question.
    grounded = f"{evidence.excerpt} {evidence.title} {query}".lower()
    for term in expected_terms:
        assert term in guidance.lower(), f"{term!r} missing from the answer"

    # Section headings are the composer's own scaffolding; the claims are the
    # bulleted and numbered lines beneath them.
    claims = " ".join(
        line
        for line in guidance.split(CITATION_HEADING)[0].splitlines()
        if line.startswith("- ") or (line[:1].isdigit() and ". " in line[:4])
    ).lower()

    invented = {
        word
        for word in claims.replace("-", " ").replace(".", " ").split()
        if len(word) > 6 and word.isalpha() and word not in grounded
    }
    # Connective wording the composer legitimately adds around the passage.
    allowed = {"procedure", "instructions", "reported", "following", "described"}
    assert not invented - allowed, f"answer used words absent from its source: {sorted(invented)}"


def test_answer_does_not_copy_a_raw_table_row() -> None:
    """Fault tables are extracted as pipe-delimited rows; answers are prose."""
    evidence = Evidence(
        source="manual",
        title="ERROR MESSAGES (ACTIVE ALARMS)",
        excerpt=(
            "8 | ERROR 15 SAFETY CIRCUIT | Interruption of the safety device sequence. | "
            "Failure of the monitoring signal of safeties devices. |"
        ),
        page=81,
        score=0.99,
    )

    guidance = _manual_guidance_sentence(evidence, "What is ERROR 15")

    assert "|" not in guidance
    assert CITATION_HEADING in guidance


def test_structured_answer_composer_renders_sections_and_context() -> None:
    composer = StructuredAnswerComposer(
        machine_label="AROL M - EURO VP - IES (Unavailable)",
        route=["doc-agent", "telemetry-agent"],
    )
    composer.add_bullets("Telemetry", ["Current telemetry is warning."])
    composer.add_raw("Manual summary\n- Check the cited procedure.\nCitation\n- Page 81")

    rendered = composer.render()
    context = composer.to_dict()

    assert rendered.startswith("Checked: AROL M - EURO VP - IES")
    assert "Route:" not in rendered
    assert "Telemetry\n- Current telemetry is warning." in rendered
    assert context["schemaVersion"] == "answer-draft/v1"
    assert context["summary"]["route"] == ["doc-agent", "telemetry-agent"]
    assert [section["title"] for section in context["sections"]] == [
        "Telemetry",
        "Manual summary",
    ]


def test_answer_node_sends_structured_answer_to_provider() -> None:
    provider = _CapturingProvider()
    state = GraphState(
        session_id="quality-session",
        machine_id="euro-vp-2019-01",
        user_message="What is ERROR 15?",
        agent_trace=["supervisor", "doc-agent"],
        intents=["documentation"],
        evidence=[
            Evidence(
                source="manual",
                title="ERROR MESSAGES (ACTIVE ALARMS)",
                excerpt=(
                    "8 | ERROR 15 SAFETY CIRCUIT | Interruption of the safety device sequence. "
                    "Sometimes it can be displayed because the doors are not correctly closed."
                ),
                page=81,
                score=0.99,
            )
        ],
    )

    answer_node(state, "AROL M - EURO VP - IES (Unavailable)", None, None, provider)

    structured = provider.context["structuredAnswer"]
    assert structured["schemaVersion"] == "answer-draft/v1"
    assert structured["summary"]["route"] == ["doc-agent"]
    assert structured["sections"][0]["title"] == "Manual summary"
    assert provider.context["draft"] == state.answer


class _CapturingProvider:
    name = "capturing"

    def __init__(self) -> None:
        self.context = {}

    def synthesize(self, context: dict) -> str:
        self.context = context
        return str(context["draft"])
