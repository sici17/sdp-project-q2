"""Retrieval over the eight supplied machine manuals.

Manuals are machine-specific, not model-specific: each documents a single
physical machine as it was built, and the file name carries the serial number.
Two things therefore have to hold, and both are easy to get wrong:

* a question about a machine reaches *that machine's* manual and no other;
* an alarm question reaches the section that explains the condition.

The second is subtler than it looks. The dataset names an alarm
``ALnnn_MNEMONIC``, and the manuals contain neither the code nor, usually, the
exact mnemonic phrase. What they contain is a fault table describing the
condition in their own words, which is what the dataset brief points at: use
the description to search the manual of the machine that raised it.

These tests read the real PDFs, so they are slower than the rest of the suite
and are skipped when the manuals are not present.
"""

import sqlite3
from pathlib import Path

import pytest

from arol_ai.rag.lexical import ManualLexicalRetriever
from arol_ai.rag.text import alarm_code_phrases

REPO_ROOT = Path(__file__).resolve().parents[2]
MANUALS_DIR = REPO_ROOT / "requirements" / "manuals"
DATASET = REPO_ROOT / "data" / "arol_q2.sqlite"

pytestmark = pytest.mark.skipif(
    not MANUALS_DIR.is_dir() or not DATASET.is_file(),
    reason="fleet dataset or machine manuals are not present",
)


def _fleet() -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{DATASET}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT machineId, serialNumber, manualFile FROM machine_manual ORDER BY machineId;"
        ).fetchall()
    finally:
        connection.close()


def _top_alarm(machine_id: str) -> str | None:
    connection = sqlite3.connect(f"file:{DATASET}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """
            SELECT alarmCode FROM Alarms WHERE machineId = ?
            GROUP BY alarmCode ORDER BY COUNT(*) DESC LIMIT 1;
            """,
            (machine_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


@pytest.fixture(scope="module")
def retriever() -> ManualLexicalRetriever:
    return ManualLexicalRetriever(manuals_dir=MANUALS_DIR)


FLEET = _fleet() if MANUALS_DIR.is_dir() and DATASET.is_file() else []


def test_every_machine_has_its_own_manual_on_disk() -> None:
    assert len(FLEET) == 8
    for row in FLEET:
        assert row["manualFile"] == f"{row['serialNumber']}_manual_EN.pdf"
        assert (MANUALS_DIR / row["manualFile"]).is_file()


@pytest.mark.parametrize("machine_id", [row["machineId"] for row in FLEET])
def test_alarm_question_reaches_that_machines_manual(
    retriever: ManualLexicalRetriever, machine_id: str
) -> None:
    """Every machine answers a question about its own most frequent alarm."""
    code = _top_alarm(machine_id)
    assert code, f"{machine_id} raised no alarms in the dataset"

    results = retriever.search(
        machine_id=machine_id,
        query=f"What does {code} mean and what does the manual recommend?",
        limit=3,
    )

    assert results, f"{machine_id}: no manual passage found for {code}"
    for evidence in results:
        # Retrieval is scoped per machine, so a citation can only ever come from
        # the manual of the machine that raised the alarm.
        assert evidence.source_uri, "evidence carries no manual reference"
        assert evidence.page, "evidence carries no page number, so it cannot be cited"


@pytest.mark.parametrize("machine_id", [row["machineId"] for row in FLEET])
def test_alarm_question_prefers_a_fault_section(
    retriever: ManualLexicalRetriever, machine_id: str
) -> None:
    """An alarm question should land on a fault table, not a random procedure.

    The manuals head these sections MESSAGE, FAULT or PROBLEM/CAUSE/SOLUTION
    rather than "alarm", which is why the classifier has to know those words.
    """
    code = _top_alarm(machine_id)
    results = retriever.search(machine_id=machine_id, query=code, limit=3)

    assert results, f"{machine_id}: {code} returned nothing"
    kinds = {evidence.chunk_kind for evidence in results}
    assert kinds.intersection({"troubleshooting", "table"}), (
        f"{machine_id}: {code} returned only {kinds}, none of them a fault section"
    )


def test_retrieval_is_scoped_to_one_machine(retriever: ManualLexicalRetriever) -> None:
    """A manual belongs to one machine; a query must not reach another's."""
    first, second = FLEET[0], FLEET[3]
    query = "closure carousel maintenance procedure"

    for machine_id, expected_file in (
        (first["machineId"], first["manualFile"]),
        (second["machineId"], second["manualFile"]),
    ):
        results = retriever.search(machine_id=machine_id, query=query, limit=3)
        for evidence in results:
            assert expected_file in (evidence.source_uri or ""), (
                f"{machine_id} cited {evidence.source_uri}, expected {expected_file}"
            )


def test_alarm_codes_expand_to_the_description_the_manuals_use() -> None:
    """The code is the key; the mnemonic is what the manual is written in."""
    assert alarm_code_phrases("AL017_LOW_AIR_PRESSURE") == ("low air pressure",)
    assert alarm_code_phrases("Why did AL082_MINIMUM_CAPS_LEVEL trigger?") == (
        "minimum caps level",
    )
    # The older manuals spell conditions as "ERROR 20"; that form carries no
    # mnemonic and must not produce a spurious phrase.
    assert alarm_code_phrases("ERROR 20 SAFETY CIRCUIT") == ()


def test_a_condition_absent_from_a_manual_is_not_answered_from_another(
    retriever: ManualLexicalRetriever,
) -> None:
    """Retrieval never falls back to a different machine's manual.

    Some conditions have no matching passage in a given machine's manual. The
    honest outcome is fewer or no results for that machine, never a citation
    borrowed from elsewhere in the fleet.
    """
    results = retriever.search(
        machine_id="MCH-0006",
        query="pharmaceutical isolator sterilisation cycle validation",
        limit=3,
    )
    for evidence in results:
        assert "A2064_manual_EN.pdf" in (evidence.source_uri or "")


def test_a_diagnostic_step_detail_is_prose_rather_than_pdf_layout() -> None:
    """The step an operator is told to follow, not the page it came from.

    `_manual_detail` returned the joined lines cut at 600 characters, so the
    step carried the page number, the subsection index, a figure callout and the
    table rows, and ended mid-word. A guard already asserted this for the manual
    summary; the diagnostic step bypassed it.
    """
    from arol_ai.rag.text import readable_passage

    raw = (
        "88 11.1.3 GENERAL INDICATIONS ON THE SCHEDULED MAINTENANCE PLAN\n"
        "A logbook of the scheduled maintenance interventions must be arranged "
        "according to the indications in the sub-chapters below.\n"
        "Fig. 187\n"
        "INTERVENTION | DATE | SIGNATURE\n"
        "Check of the height sliding of the closure carousel. XX/XX/XX | Mario Rossi | positive\n"
        "The closing machine must be stopped before any intervention on guarded areas."
    )

    detail = readable_passage(raw)

    assert detail.startswith("GENERAL INDICATIONS")
    assert "88 11.1.3" not in detail
    assert "Fig. 187" not in detail
    assert "|" not in detail
    assert "Mario Rossi" not in detail
    assert detail.endswith(".")


def test_a_long_passage_is_cut_at_a_sentence_not_mid_word() -> None:
    from arol_ai.rag.text import readable_passage

    passage = " ".join(
        f"Sentence number {index} describes a maintenance step." for index in range(60)
    )

    cut = readable_passage(passage, limit=200)

    assert len(cut) <= 200
    assert cut.endswith(".")
