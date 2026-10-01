from typing import Any, Dict, List, Optional
from src.mcp.domains.calendar_module import CalendarDomainStore, CALENDAR_TOOL_METADATA
from src.schemas.tool_metadata import ToolMetadata

class CareMCPServer:
    """
    Unified Namespaced MCP Server for CARE.
    Exposes discovery tools for read-only planning and gated state-changing tools for execution.
    """
    def __init__(self, calendar_store: Optional[CalendarDomainStore] = None):
        self.calendar_store = calendar_store or CalendarDomainStore()
        self.metadata_registry: Dict[str, ToolMetadata] = {
            **CALENDAR_TOOL_METADATA
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
        else:
            raise NotImplementedError(f"Operation {operation} not supported in current phase.")
