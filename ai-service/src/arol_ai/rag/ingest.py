import argparse

from arol_ai.rag.ingestion import ingest_manuals


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest manual PDFs into the document RAG index.")
    parser.add_argument(
        "--reset", action="store_true", help="Delete and recreate the collection first."
    )
    args = parser.parse_args()

    count = ingest_manuals(reset=args.reset)
    print(f"Ingested {count} manual chunk(s).")


if __name__ == "__main__":
    main()
