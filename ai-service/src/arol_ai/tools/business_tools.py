from dataclasses import dataclass

from arol_ai.agents.business_agent import (
    build_business_evidence,
    build_order_evidence,
    build_service_history_evidence,
)
from arol_ai.domain.models import OrderRecord, QuoteRecord, ServiceContract, ServiceHistoryRecord
from arol_ai.graph.state import Evidence
from arol_ai.tools.base import ToolCallRecord


@dataclass(frozen=True)
class ServiceEntitlementResult:
    evidence: Evidence | None
    tool_call: ToolCallRecord


class ServiceEntitlementTool:
    name = "business.service_entitlement"
    agent = "business-agent"

    def run(
        self,
        *,
        contract: ServiceContract | None,
        dependency_error: str | None = None,
    ) -> ServiceEntitlementResult:
        if dependency_error:
            return ServiceEntitlementResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="error",
                    input_summary="service entitlement requested",
                    output_summary=dependency_error,
                ),
            )
        if contract is None:
            return ServiceEntitlementResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="empty",
                    input_summary="service entitlement requested",
                    output_summary="No service entitlement record was available.",
                ),
            )

        return ServiceEntitlementResult(
            evidence=build_business_evidence(contract),
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary=f"service standing for {contract.machine_id}",
                output_summary=(
                    f"Delivered {contract.delivery_date or 'date unrecorded'}; "
                    f"{contract.open_ticket_count} open maintenance ticket(s)."
                ),
            ),
        )


@dataclass(frozen=True)
class BusinessHistoryResult:
    evidence: Evidence | None
    tool_call: ToolCallRecord


class OrderHistoryTool:
    name = "business.list_orders"
    agent = "business-agent"

    def run(
        self,
        *,
        orders: list[OrderRecord],
        dependency_error: str | None = None,
    ) -> BusinessHistoryResult:
        if dependency_error:
            return BusinessHistoryResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="error",
                    input_summary="machine order history requested",
                    output_summary=dependency_error,
                ),
            )
        if not orders:
            return BusinessHistoryResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="empty",
                    input_summary="machine order history requested",
                    output_summary="No machine orders were available.",
                ),
            )
        return BusinessHistoryResult(
            evidence=build_order_evidence(orders),
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary="machine order history requested",
                output_summary=f"Returned {len(orders)} order record(s).",
            ),
        )


class ServiceHistoryTool:
    name = "business.list_service_history"
    agent = "business-agent"

    def run(
        self,
        *,
        records: list[ServiceHistoryRecord],
        dependency_error: str | None = None,
    ) -> BusinessHistoryResult:
        if dependency_error:
            return BusinessHistoryResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="error",
                    input_summary="machine service history requested",
                    output_summary=dependency_error,
                ),
            )
        if not records:
            return BusinessHistoryResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="empty",
                    input_summary="machine service history requested",
                    output_summary="No maintenance or service records were available.",
                ),
            )
        return BusinessHistoryResult(
            evidence=build_service_history_evidence(records),
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary="machine service history requested",
                output_summary=f"Returned {len(records)} service record(s).",
            ),
        )


class QuoteHistoryTool:
    """Quotations and how their latest revision changed.

    Reports the commercial state the dataset actually records rather than
    implying an offer is live: a quote whose current revision is Rejected or
    Expired is closed, and saying so is the useful answer.
    """

    name = "business.list_quotes"
    agent = "business-agent"

    def run(
        self,
        *,
        quotes: list[QuoteRecord],
        dependency_error: str | None = None,
    ) -> BusinessHistoryResult:
        if dependency_error:
            return BusinessHistoryResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="error",
                    input_summary="company quotation history requested",
                    output_summary=dependency_error,
                ),
            )
        if not quotes:
            return BusinessHistoryResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="empty",
                    input_summary="company quotation history requested",
                    output_summary="No quotations were issued to this company.",
                ),
            )

        return BusinessHistoryResult(
            evidence=build_quote_evidence(quotes),
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary="company quotation history requested",
                output_summary=f"Returned {len(quotes)} quotation(s).",
            ),
        )


def build_quote_evidence(quotes: list[QuoteRecord]) -> Evidence:
    """Summarize quotations, leading with the most recent."""
    lines = []
    for quote in quotes[:5]:
        state = quote.current_revision_status or "unknown state"
        total = (
            f"{quote.current_total:,.2f} {quote.currency}"
            if quote.current_total is not None and quote.currency
            else "no priced lines"
        )
        detail = (
            f"{quote.quote_id}: {quote.description or 'quotation'} - "
            f"revision {quote.current_revision_number} of {quote.revision_count}, "
            f"{state}, {total}"
        )
        if quote.expired:
            detail += ", past its validity date"
        lines.append(detail)

    return Evidence(
        source="business",
        title="Quotation history",
        excerpt=" | ".join(lines),
        confidence=0.9,
    )
