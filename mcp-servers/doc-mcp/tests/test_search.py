import unittest
from unittest.mock import patch

from retrieval import search


class HybridManualSearchTest(unittest.TestCase):
    def test_embedded_domain_phrase_outranks_generic_expansion_terms(self) -> None:
        query = "The panel reports FOIL CHECK INTERVENTION. What should I inspect?"
        foil = {
            "chunkId": "foil",
            "title": "MESSAGE",
            "excerpt": "26 FOIL CHECK INTERVENTION: inspect the foil presence check device.",
            "score": 0.8,
            "chunkKind": "safety",
            "topics": ["emergency", "foil"],
        }
        generic_alarm = {
            "chunkId": "generic-alarm",
            "title": "MESSAGE",
            "excerpt": "Check the alarm message and inspect the device.",
            "score": 0.9,
            "chunkKind": "troubleshooting",
            "topics": ["alarm"],
        }

        ranked = search._rerank_documents(query, [generic_alarm, foil])

        self.assertEqual(ranked[0]["chunkId"], "foil")

    def test_exact_alarm_code_lexical_hit_skips_embedding_dependency(self) -> None:
        lexical = {
            "source": "manual",
            "title": "ERROR 20 SAFETY CIRCUIT",
            "excerpt": "ERROR 20 SAFETY CIRCUIT: keep guards and interlocks enabled.",
            "page": 81,
            "chunkId": "alarm-20",
            "score": 0.96,
            "confidence": 0.96,
            "chunkKind": "troubleshooting",
            "topics": ["safety"],
            "alarmCodes": ["ERROR 20"],
            "safetyLevel": "safety-critical",
        }
        with (
            patch.object(search, "_lexical_search", return_value=[lexical]),
            patch.object(
                search,
                "_embed_query",
                side_effect=AssertionError("embedding must not be called"),
            ),
        ):
            result = search.search_manual(
                "euro-vp-2019-01",
                "What does ERROR 20 mean?",
            )

        self.assertEqual([item["chunkId"] for item in result], ["alarm-20"])
        self.assertEqual(result[0]["safetyLevel"], "safety-critical")

    def test_hybrid_search_merges_and_reranks_vector_metadata(self) -> None:
        vector_response = {
            "result": {
                "points": [
                    {
                        "score": 0.89,
                        "payload": {
                            "machineId": "euro-vp-2019-01",
                            "section": "Torque procedure",
                            "text": "Inspect the capper head and verify torque.",
                            "pageStart": 42,
                            "sourceUri": "/manuals/capper.pdf",
                            "chunkId": "torque-42",
                            "chunkKind": "procedure",
                            "topics": ["torque"],
                            "alarmCodes": ["TORQUE_HIGH"],
                            "safetyLevel": "technician",
                        },
                    }
                ]
            }
        }
        with (
            patch.object(search, "_lexical_search", return_value=[]),
            patch.object(search, "_embed_query", return_value=[0.1, 0.2]),
            patch.object(search, "_post_json", return_value=vector_response),
        ):
            result = search.search_manual(
                "euro-vp-2019-01",
                "torque inspection procedure",
            )

        self.assertEqual(result[0]["chunkId"], "torque-42")
        self.assertEqual(result[0]["chunkKind"], "procedure")
        self.assertEqual(result[0]["topics"], ["torque"])
        self.assertEqual(result[0]["alarmCodes"], ["TORQUE_HIGH"])
        self.assertEqual(result[0]["safetyLevel"], "technician")

    def test_source_quality_rejects_course_forms_and_cleans_technical_text(self) -> None:
        footer = (
            "AROL S.p.A. - teaching copy, Politecnico di Torino, "
            "System and Device Programming. Do not redistribute."
        )
        documents = [
            {"title": "Manual", "excerpt": footer},
            {
                "title": "Report of the training",
                "excerpt": f"Trainer/s signature/s: Mr. ______\n{footer}",
            },
            {
                "title": "Torque check",
                "excerpt": f"Verify the guarded torque setting.\n{footer}",
                "chunkId": "technical",
            },
        ]

        result = search._sanitize_documents(documents)

        self.assertEqual([item["chunkId"] for item in result], ["technical"])
        self.assertEqual(result[0]["excerpt"], "Verify the guarded torque setting.")


if __name__ == "__main__":
    unittest.main()
