from dataclasses import dataclass, field


@dataclass(frozen=True)
class AnswerSection:
    title: str
    bullets: list[str] = field(default_factory=list)
    raw: str = ""

    def render(self) -> str:
        if self.raw:
            return self.raw.strip()

        lines = [self.title]
        lines.extend(f"- {bullet}" for bullet in self.bullets if bullet)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        payload = {"title": self.title}
        if self.bullets:
            payload["bullets"] = self.bullets
        if self.raw:
            payload["raw"] = self.raw
        return payload


class StructuredAnswerComposer:
    def __init__(self, *, machine_label: str, route: list[str]) -> None:
        self.machine_label = machine_label
        self.route = route
        self.sections: list[AnswerSection] = []

    def add_bullets(self, title: str, bullets: list[str]) -> None:
        clean_bullets = [bullet.strip() for bullet in bullets if bullet and bullet.strip()]
        if clean_bullets:
            self.sections.append(AnswerSection(title=title, bullets=clean_bullets))

    def add_raw(self, raw: str) -> None:
        if raw.strip():
            self.sections.append(AnswerSection(title=_raw_title(raw), raw=raw))

    def render(self) -> str:
        header = [f"Checked: {self.machine_label}"]
        body = [section.render() for section in self.sections]
        return "\n\n".join(["\n".join(header), *body])

    def to_dict(self) -> dict:
        return {
            "schemaVersion": "answer-draft/v1",
            "summary": {
                "checkedMachine": self.machine_label,
                "route": self.route,
            },
            "sections": [section.to_dict() for section in self.sections],
        }


def _raw_title(raw: str) -> str:
    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    return first_line or "Answer"
