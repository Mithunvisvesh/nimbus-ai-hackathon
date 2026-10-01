from typing import Any, Dict, List, Optional
from src.mcp.domains.calendar_module import CalendarDomainStore, CALENDAR_TOOL_METADATA
from src.mcp.domains.tickets_module import TicketsDomainStore, TICKETS_TOOL_METADATA
from src.schemas.tool_metadata import ToolMetadata

class CareMCPServer:
    """
    Unified Namespaced MCP Server for CARE.
    Exposes discovery tools for read-only planning and gated state-changing tools for execution.
    """
    def __init__(
        self,
        calendar_store: Optional[CalendarDomainStore] = None,
        tickets_store: Optional[TicketsDomainStore] = None,
    ):
        self.calendar_store = calendar_store or CalendarDomainStore()
        self.tickets_store = tickets_store or TicketsDomainStore()
        self.metadata_registry: Dict[str, ToolMetadata] = {
            **CALENDAR_TOOL_METADATA,
            **TICKETS_TOOL_METADATA,
        }

    def get_tool_metadata(self, operation: str) -> Optional[ToolMetadata]:
        return self.metadata_registry.get(operation)

    # ---------------- Read-Only Tools (Ungated) ----------------
    def list_calendar_events(self, start_time: Optional[str] = None, end_time: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.calendar_store.list_events(start_time, end_time)

    def get_calendar_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        return self.calendar_store.get_event(event_id)

    def check_calendar_availability(self, start_time: str, end_time: str, exclude_event_id: Optional[str] = None) -> Dict[str, Any]:
        return self.calendar_store.check_availability(start_time, end_time, exclude_event_id)

    def list_tickets(self, status: Optional[str] = None, is_escalated: Optional[bool] = None) -> List[Dict[str, Any]]:
        return self.tickets_store.list_tickets(status, is_escalated)

    def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return self.tickets_store.get_ticket(ticket_id)

    # ---------------- Gated State-Changing Tools ----------------
    def dispatch_tool(
        self,
        operation: str,
        parameters: Dict[str, Any],
        plan_id: str,
        action_hash: str,
        simulate_failure: bool = False,
    ) -> Dict[str, Any]:
        """
        Dispatches a state-changing tool call. Requires plan_id and action_hash.
        """
        meta = self.get_tool_metadata(operation)
        if not meta:
            raise ValueError(f"Unknown MCP tool operation: {operation}")

        if not plan_id or not action_hash:
            raise PermissionError(f"Operation {operation} requires verified plan_id and action_hash.")

        if operation == "calendar.update_event":
            return self.calendar_store.update_event(
                event_id=parameters["event_id"],
                start_time=parameters.get("start_time"),
                end_time=parameters.get("end_time"),
                title=parameters.get("title"),
                plan_id=plan_id,
                action_hash=action_hash,
                simulate_failure=simulate_failure,
            )
        elif operation == "tickets.update_status":
            new_status = parameters.get("new_status") or parameters.get("status") or "closed"
            return self.tickets_store.update_status(
                ticket_id=parameters["ticket_id"],
                new_status=new_status,
                resolution_notes=parameters.get("resolution_notes"),
                clear_resolution_notes=parameters.get("clear_resolution_notes", False),
                plan_id=plan_id,
                action_hash=action_hash,
                simulate_failure=simulate_failure,
            )
        elif operation == "tickets.reopen_ticket":
            return self.tickets_store.reopen_ticket(
                ticket_id=parameters["ticket_id"],
                reason=parameters.get("reason"),
                plan_id=plan_id,
                action_hash=action_hash,
                simulate_failure=simulate_failure,
            )
        else:
            raise NotImplementedError(f"Operation {operation} not supported in current phase.")
