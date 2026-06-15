from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from arol_ai.rag.metadata import chunk_kind, extract_alarm_codes, extract_topics, safety_level


@dataclass(frozen=True)
class ManualManifestEntry:
    machine_id: str
    title: str
    version: str
    language: str
    file_name: str
    source_uri: str
    machine_model: str = "Unavailable"
    company_id: str | None = None
    serial_number: str = "Unavailable"
    plant: str = "Unavailable"
    status: str = "offline"
    related_machine_ids: tuple[str, ...] = ()
    #: Leading pages before the manual's own page 1. Citations name the PDF
    #: index; the number printed on the page is that index minus this. Zero
    #: when the two agree, or when the manual does not expose its numbering.
    printed_page_offset: int = 0

    @classmethod
    def from_dict(cls, payload: dict) -> "ManualManifestEntry":
        machine = payload.get("machine") or {}
        return cls(
            machine_id=payload["machineId"],
            title=payload["title"],
            version=payload["version"],
            language=payload["language"],
            file_name=payload["fileName"],
            source_uri=payload["sourceUri"],
            machine_model=machine.get("model", payload.get("model", "Unavailable")),
            company_id=machine.get("companyId", payload.get("companyId")),
            serial_number=machine.get(
                "serialNumber",
                payload.get("serialNumber", "Unavailable"),
            ),
            plant=machine.get("plant", payload.get("plant", "Unavailable")),
            status=machine.get("status", payload.get("status", "offline")),
            related_machine_ids=tuple(payload.get("relatedMachineIds", [])),
            printed_page_offset=int(payload.get("printedPageOffset") or 0),
        )

    def pdf_path(self, manuals_dir: Path) -> Path:
        return manuals_dir / self.file_name

    def to_dict(self) -> dict:
        return {
            "machineId": self.machine_id,
            "title": self.title,
            "version": self.version,
            "language": self.language,
            "fileName": self.file_name,
            "sourceUri": self.source_uri,
            "relatedMachineIds": list(self.related_machine_ids),
        }

    def machine_dict(self) -> dict:
        return {
            "id": self.machine_id,
            "serialNumber": self.serial_number,
            "model": self.machine_model,
            "plant": self.plant,
            "status": self.status,
        }


@dataclass(frozen=True)
class ExtractedPage:
    page: int
    text: str
    tables: list[str]


@dataclass(frozen=True)
class ManualChunk:
    id: str
    machine_id: str
    title: str
    manual_version: str
    language: str
    source_uri: str
    section: str
    page_start: int
    page_end: int
    text: str
    chunk_kind: str = "reference"
    topics: tuple[str, ...] = ()
    alarm_codes: tuple[str, ...] = ()
    safety_level: str = "operator"

    @classmethod
    def create(
        cls,
        *,
        manifest: ManualManifestEntry,
        section: str,
        page_start: int,
        page_end: int,
        text: str,
    ) -> "ManualChunk":
        chunk_id = _chunk_id(
            manifest.machine_id,
            manifest.version,
            manifest.language,
            manifest.source_uri,
            str(page_start),
            section,
            text,
        )

        metadata_text = f"{section}\n{text}"

        return cls(
            id=chunk_id,
            machine_id=manifest.machine_id,
            title=manifest.title,
            manual_version=manifest.version,
            language=manifest.language,
            source_uri=manifest.source_uri,
            section=section,
            page_start=page_start,
            page_end=page_end,
            text=text,
            chunk_kind=chunk_kind(section=section, text=text),
            topics=extract_topics(section=section, text=text),
            alarm_codes=extract_alarm_codes(metadata_text),
            safety_level=safety_level(section=section, text=text),
        )

    def payload(self) -> dict:
        return {
            "chunkId": self.id,
            "machineId": self.machine_id,
            "title": self.title,
            "manualVersion": self.manual_version,
            "language": self.language,
            "sourceUri": self.source_uri,
            "section": self.section,
            "pageStart": self.page_start,
            "pageEnd": self.page_end,
            "text": self.text,
            "chunkKind": self.chunk_kind,
            "topics": list(self.topics),
            "alarmCodes": list(self.alarm_codes),
            "safetyLevel": self.safety_level,
        }


def _chunk_id(*parts: str | None) -> str:
    """A stable identifier for one manual chunk.

    Parts may legitimately be absent: the fleet dataset carries no manual
    revision, so ``version`` is null rather than invented. An empty marker keeps
    the identifier stable and distinct from a manual that does declare one.
    """
    digest = sha256("::".join(part or "" for part in parts).encode("utf8")).hexdigest()
    return str(uuid5(NAMESPACE_URL, digest))


@dataclass(frozen=True)
class ManualSearchScope:
    machine_id: str
    manual_version: str | None
    language: str | None
