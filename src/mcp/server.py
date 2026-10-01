from typing import Any, Dict, List, Optional
from src.mcp.domains.calendar_module import CalendarDomainStore, CALENDAR_TOOL_METADATA
from src.mcp.domains.tickets_module import TicketsDomainStore, TICKETS_TOOL_METADATA
from src.schemas.tool_metadata import ToolMetadata
from src.journal.db import ActionJournalDB

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
        self._authorization_token = object()
        self.calendar_store._bind_authorization_token(self._authorization_token)
        self.tickets_store._bind_authorization_token(self._authorization_token)
        self._journal: Optional[ActionJournalDB] = None
        self.metadata_registry: Dict[str, ToolMetadata] = {
            **CALENDAR_TOOL_METADATA,
            **TICKETS_TOOL_METADATA,
        }

    def get_tool_metadata(self, operation: str) -> Optional[ToolMetadata]:
        return self.metadata_registry.get(operation)

    def bind_journal(self, journal_db: ActionJournalDB) -> None:
        """Bind the executor's durable plan journal as mutation authorization authority."""
        self._journal = journal_db

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
        action_id: Optional[str] = None,
        dispatch_token: Optional[str] = None,
        dispatch_kind: str = "execute",
    ) -> Dict[str, Any]:
        """
        Dispatches a state-changing tool call. Requires plan_id and action_hash.
        """
        meta = self.get_tool_metadata(operation)
        if not meta:
            raise ValueError(f"Unknown MCP tool operation: {operation}")
        if meta.read_only:
            raise PermissionError(f"Operation '{operation}' is read-only and cannot use the mutation dispatch path.")

        if not self._journal:
            raise PermissionError("No durable plan journal is bound; state-changing tools are disabled.")
        if not action_id:
            raise PermissionError("action_id is required for server-side mutation authorization.")
        resource_id = parameters.get("event_id") or parameters.get("ticket_id")
        if not resource_id:
            raise PermissionError("A resource identifier is required for mutation authorization.")
        self._journal.authorize_and_claim_tool_call(
            plan_id=plan_id,
            action_hash=action_hash,
            action_id=action_id,
            operation=operation,
            resource_id=resource_id,
            parameters=parameters,
            dispatch_token=dispatch_token,
            dispatch_kind=dispatch_kind,
        )

        if operation == "calendar.update_event":
            return self.calendar_store.update_event(
                event_id=parameters["event_id"],
                start_time=parameters.get("start_time"),
                end_time=parameters.get("end_time"),
                title=parameters.get("title"),
                plan_id=plan_id,
                action_hash=action_hash,
                simulate_failure=simulate_failure,
                _authorization_token=self._authorization_token,
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
                _authorization_token=self._authorization_token,
            )
        elif operation == "tickets.reopen_ticket":
            return self.tickets_store.reopen_ticket(
                ticket_id=parameters["ticket_id"],
                reason=parameters.get("reason"),
                plan_id=plan_id,
                action_hash=action_hash,
                simulate_failure=simulate_failure,
                _authorization_token=self._authorization_token,
            )
        else:
            raise NotImplementedError(f"Operation {operation} not supported in current phase.")
