from dataclasses import dataclass

from arol_ai.alarm_codes import contains_alarm_code, extract_alarm_code
from arol_ai.domain.models import Manual
from arol_ai.graph.state import Evidence
from arol_ai.mcp import McpClient
from arol_ai.rag.evidence_quality import sanitize_manual_evidence
from arol_ai.rag.models import ManualSearchScope
from arol_ai.rag.retriever import ManualVectorRetriever
from arol_ai.rag.text import alarm_code_phrases, carries_alarm_description
from arol_ai.tools.base import ToolCallRecord


@dataclass(frozen=True)
class ManualSearchResult:
    evidence: list[Evidence]
    tool_call: ToolCallRecord


class ManualSearchTool:
    name = "manual.search"
    agent = "doc-agent"

    def __init__(
        self,
        *,
        retriever: ManualVectorRetriever | None = None,
        rag_enabled: bool = False,
        mcp_client: McpClient | None = None,
    ) -> None:
        self.retriever = retriever
        self.rag_enabled = rag_enabled
        self.mcp_client = mcp_client

    def run(
        self,
        *,
        machine_id: str,
        manual: Manual | None,
        query: str,
        operator_query: str | None = None,
        limit: int = 6,
    ) -> ManualSearchResult:
        """Search this machine's manual.

        ``query`` is what retrieval searches for, and the caller may have
        enriched it with the machine's recorded alarm. ``operator_query`` is
        what the operator actually typed, and it is what the exact-code filter
        keys on: a code the platform added is not a code the operator asked
        about. They are the same string when the caller does not enrich.
        """
        if not self.rag_enabled:
            return self._empty_result(
                status="empty",
                query=query,
                output_summary="Document RAG is disabled, so no manual citations were searched.",
            )

        if self.mcp_client is not None:
            result = self._run_mcp_search(
                machine_id=machine_id,
                manual=manual,
                query=query,
                limit=limit,
            )
        else:
            result = self._run_vector_search(
                machine_id=machine_id,
                manual=manual,
                query=query,
                limit=limit,
            )
        result = _filter_non_operational_evidence(result)
        return _filter_exact_alarm_evidence(
            result,
            query=operator_query if operator_query is not None else query,
        )

    def _run_mcp_search(
        self,
        *,
        machine_id: str,
        manual: Manual | None,
        query: str,
        limit: int,
    ) -> ManualSearchResult:
        try:
            evidence: list[Evidence] = []
            for scope in _manual_search_scopes(machine_id=machine_id, manual=manual):
                payload = self.mcp_client.call_tool(
                    self.name,
                    {
                        "machineId": scope.machine_id,
                        "query": query,
                        "manualVersion": scope.manual_version,
                        "language": scope.language,
                        "limit": limit,
                    },
                )
                if isinstance(payload, list):
                    evidence.extend(
                        _evidence_from_mcp(item) for item in payload if isinstance(item, dict)
                    )
            evidence = _dedupe_evidence(evidence)[:limit]
        except Exception as exc:
            return self._empty_result(
                status="error",
                query=query,
                output_summary=f"Document MCP search failed: {exc}",
            )

        if not evidence:
            return self._empty_result(
                status="empty",
                query=query,
                output_summary="No matching manual citations were returned by Document MCP.",
            )
        return ManualSearchResult(
            evidence=evidence,
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary=f"machine manual MCP search for: {query[:160]}",
                output_summary=f"Returned {len(evidence)} MCP manual citation(s).",
            ),
        )

    def _run_vector_search(
        self,
        *,
        machine_id: str,
        manual: Manual | None,
        query: str,
        limit: int,
    ) -> ManualSearchResult:
        if self.retriever is None:
            return self._empty_result(
                status="error",
                query=query,
                output_summary="Document RAG is enabled, but no vector retriever is configured.",
            )

        try:
            scopes = _manual_search_scopes(machine_id=machine_id, manual=manual)
            if hasattr(self.retriever, "search_many"):
                evidence = self.retriever.search_many(scopes=scopes, query=query, limit=limit)
            else:
                evidence = self.retriever.search(
                    machine_id=machine_id,
                    query=query,
                    manual_version=manual.version if manual else None,
                    language=manual.language if manual else None,
                    limit=limit,
                )
        except Exception as exc:
            return self._empty_result(
                status="error",
                query=query,
                output_summary=f"Document RAG search failed: {exc}",
            )

        if not evidence:
            return self._empty_result(
                status="empty",
                query=query,
                output_summary="No matching manual citations were found in the vector index.",
            )

        return ManualSearchResult(
            evidence=evidence,
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary=f"machine manual vector search for: {query[:160]}",
                output_summary=f"Returned {len(evidence)} vector manual citation(s).",
            ),
        )

    def _empty_result(self, *, status: str, query: str, output_summary: str) -> ManualSearchResult:
        return ManualSearchResult(
            evidence=[],
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status=status,
                input_summary=f"machine manual vector search for: {query[:160]}",
                output_summary=output_summary,
            ),
        )


def _manual_search_scopes(*, machine_id: str, manual: Manual | None) -> list[ManualSearchScope]:
    manual_matches_machine = manual is not None and manual.machine_id == machine_id
    scopes = [
        ManualSearchScope(
            machine_id=machine_id,
            manual_version=manual.version if manual_matches_machine else None,
            language=manual.language if manual_matches_machine else None,
        )
    ]

    if manual:
        if manual.machine_id != machine_id:
            scopes.append(
                ManualSearchScope(
                    machine_id=manual.machine_id,
                    manual_version=manual.version,
                    language=manual.language,
                )
            )

        scopes.extend(
            ManualSearchScope(
                machine_id=related.machine_id,
                manual_version=related.version,
                language=related.language,
            )
            for related in manual.related_manuals
        )

    return scopes


def _evidence_from_mcp(payload: dict) -> Evidence:
    return Evidence(
        source=str(payload.get("source") or "manual"),
        title=str(payload.get("title") or payload.get("section") or "Manual evidence"),
        excerpt=str(payload.get("excerpt") or ""),
        page=payload.get("page"),
        confidence=payload.get("confidence"),
        manual_version=payload.get("manualVersion"),
        language=payload.get("language"),
        source_uri=payload.get("sourceUri"),
        chunk_id=payload.get("chunkId"),
        section=payload.get("section"),
        score=payload.get("score"),
        chunk_kind=payload.get("chunkKind"),
        topics=tuple(payload.get("topics") or ()),
        alarm_codes=tuple(payload.get("alarmCodes") or ()),
        safety_level=payload.get("safetyLevel"),
    )


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    deduped: list[Evidence] = []
    seen: set[str] = set()
    for item in sorted(
        items,
        key=lambda evidence: evidence.score or evidence.confidence or 0.0,
        reverse=True,
    ):
        key = item.chunk_id or f"{item.source_uri}:{item.page}:{item.excerpt}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _filter_non_operational_evidence(result: ManualSearchResult) -> ManualSearchResult:
    if not result.evidence:
        return result

    evidence = sanitize_manual_evidence(result.evidence)
    if evidence:
        return ManualSearchResult(evidence=evidence, tool_call=result.tool_call)

    return ManualSearchResult(
        evidence=[],
        tool_call=ToolCallRecord.create(
            name=result.tool_call.name,
            agent=result.tool_call.agent,
            status="empty",
            input_summary=result.tool_call.input_summary,
            output_summary=(
                "Search results contained only copyright, course notice, training-form, "
                "blank-page, or table-of-contents content; they were rejected as sources."
            ),
        ),
    )


def _filter_exact_alarm_evidence(
    result: ManualSearchResult,
    *,
    query: str,
) -> ManualSearchResult:
    """Do not cite a semantic neighbour when the operator supplied an exact code.

    ``query`` must be the operator's own message. An enriched retrieval query
    carries the machine's recorded alarm, and reading the code out of that
    would apply this rejection to questions that named no code at all.
    """
    requested_code = extract_alarm_code(query)
    if requested_code is None or not result.evidence:
        return result

    matching = [
        item
        for item in result.evidence
        if any(
            contains_alarm_code(value, requested_code)
            for value in (
                item.title,
                item.section,
                item.excerpt,
                *item.alarm_codes,
            )
        )
    ]
    if matching:
        return ManualSearchResult(evidence=matching, tool_call=result.tool_call)

    # The supplied manuals never print an AL number, so an exact match is
    # impossible there and this filter would otherwise silence the manual for
    # every alarm question. Where the code carries its own description, that
    # description is what the manual is written in: a passage about low air
    # pressure does explain AL017_LOW_AIR_PRESSURE. A bare code carries no
    # description, so nothing can be shown to be about it and the rejection
    # below still stands.
    described = [
        item
        for item in result.evidence
        if carries_alarm_description(
            " ".join(part for part in (item.title, item.section, item.excerpt) if part),
            alarm_code_phrases(query),
        )
    ]
    if described:
        return ManualSearchResult(evidence=described, tool_call=result.tool_call)

    return ManualSearchResult(
        evidence=[],
        tool_call=ToolCallRecord.create(
            name=result.tool_call.name,
            agent=result.tool_call.agent,
            status="empty",
            input_summary=result.tool_call.input_summary,
            output_summary=(
                f"No passage grounded in the physical condition for {requested_code} was found; "
                "unrelated manual passages were rejected."
            ),
        ),
    )
