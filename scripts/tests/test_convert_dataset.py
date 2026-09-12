"""Tests for the fleet dataset converter.

These cover the rules the dataset brief states, which the rest of the platform
then relies on: per-machine configuration parsing, the manual join by serial
number, and the commercial lifecycle living on quote revisions.
"""

import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from convert_dataset import (  # noqa: E402
    MANUAL_FILE_TEMPLATE,
    PLATFORM_TODAY,
    parse_configuration_profile,
    validate,
)

DATABASE = REPO_ROOT / "data" / "arol_q2.sqlite"
MANUALS = REPO_ROOT / "requirements" / "manuals"


class ConfigurationProfileTest(unittest.TestCase):
    """Nominal rate, voltage and head count live in configurationProfile."""

    def test_parses_a_multi_head_profile(self) -> None:
        parsed = parse_configuration_profile(
            "Twin chute / 20 heads / 40000 bph / 400V-60Hz / PK 314 chuck"
        )
        self.assertEqual(parsed["nominalRateBph"], 40000)
        self.assertEqual(parsed["headsCount"], 20)
        self.assertEqual(parsed["supplyVoltage"], 400)
        self.assertEqual(parsed["supplyFrequencyHz"], 60)

    def test_single_head_turret_counts_as_one_head(self) -> None:
        parsed = parse_configuration_profile(
            "Single head turret / 3000 bph / 380V-50Hz / PET 500 ml"
        )
        self.assertEqual(parsed["headsCount"], 1)
        self.assertEqual(parsed["nominalRateBph"], 3000)

    def test_missing_profile_yields_no_derived_limits(self) -> None:
        self.assertEqual(
            parse_configuration_profile(None),
            {
                "nominalRateBph": None,
                "supplyVoltage": None,
                "supplyFrequencyHz": None,
                "headsCount": None,
            },
        )


@unittest.skipUnless(DATABASE.is_file(), "converted dataset is not present")
class ConvertedDatasetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.connection.close()

    def test_validation_reports_no_errors(self) -> None:
        errors, warnings = validate(DATABASE, MANUALS)
        self.assertEqual(errors, [])
        # The planted edge cases must stay visible rather than being silently
        # normalized away by the conversion.
        self.assertTrue(warnings)

    def test_every_machine_resolves_to_its_own_manual(self) -> None:
        rows = self.connection.execute(
            "SELECT machineId, serialNumber, manualFile FROM Machines;"
        ).fetchall()
        self.assertEqual(len(rows), 8)
        for row in rows:
            self.assertEqual(
                row["manualFile"],
                MANUAL_FILE_TEMPLATE.format(serial=row["serialNumber"]),
            )
            self.assertTrue((MANUALS / row["manualFile"]).is_file())

    def test_current_revision_is_the_highest_numbered_one(self) -> None:
        rows = self.connection.execute(
            """
            SELECT r.quoteId, r.revisionNumber, r.revisionStatus,
                   (SELECT MAX(revisionNumber) FROM QuoteRevisions x
                     WHERE x.quoteId = r.quoteId) AS maxRevision
            FROM current_quote_revision r;
            """
        ).fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["revisionNumber"], row["maxRevision"])

    def test_a_quote_whose_final_revision_was_rejected_is_represented(self) -> None:
        row = self.connection.execute(
            "SELECT revisionStatus FROM current_quote_revision WHERE quoteId = 'QTE-2025-0003';"
        ).fetchone()
        self.assertEqual(row["revisionStatus"], "Rejected")

    def test_order_content_comes_from_the_approved_revision(self) -> None:
        # OrderLines tracks fulfilment only and carries no item, quantity or
        # price, so an order's content has to come through its approved revision.
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(OrderLines);")
        }
        self.assertEqual(columns, {"orderLineId", "orderId", "fulfillmentStatus"})

        rows = self.connection.execute(
            "SELECT description, price FROM order_content WHERE orderId = 'ORD-2025-0001';"
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["price"] is not None for row in rows))

    def test_a_company_may_own_no_machines(self) -> None:
        row = self.connection.execute(
            """
            SELECT c.companyId, COUNT(m.machineId) AS machines
            FROM Companies c
            LEFT JOIN Machines m ON m.companyId = c.companyId
            GROUP BY c.companyId
            HAVING machines = 0;
            """
        ).fetchone()
        self.assertIsNotNone(row, "the dataset contains a company with users but no machines")

    def test_every_visibility_level_is_represented(self) -> None:
        levels = {
            row["visibility"]
            for row in self.connection.execute("SELECT DISTINCT visibility FROM Users;")
        }
        self.assertEqual(levels, {"full", "technician", "commercial"})

    def test_reference_date_is_recorded_for_services_to_read(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM dataset_meta WHERE key = 'platformToday';"
        ).fetchone()
        self.assertEqual(row["value"], PLATFORM_TODAY)


if __name__ == "__main__":
    unittest.main()
