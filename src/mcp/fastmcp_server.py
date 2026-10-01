"""FastMCP protocol adapter for CARE's existing namespaced tool registry."""

from typing import Any, Optional
import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastmcp import FastMCP

from src.journal.db import ActionJournalDB
from src.mcp.server import CareMCPServer


class UnknownMCPOutcomeError(RuntimeError):
    """The protocol call failed without proving whether the remote tool ran."""


def create_fastmcp_server(
    care_server: Optional[CareMCPServer] = None,
    journal_db: Optional[ActionJournalDB] = None,
) -> FastMCP:
    """Create one real MCP server backed by CARE's existing domain stores."""
    care = care_server or CareMCPServer()
    journal = journal_db or ActionJournalDB()
    care.bind_journal(journal)
    protocol = FastMCP("CARE")

    @protocol.tool(name="calendar.list_events", description="Read calendar events. Read-only discovery operation.")
    def calendar_list_events(start_time: Optional[str] = None, end_time: Optional[str] = None) -> list[dict[str, Any]]:
        return care.list_calendar_events(start_time, end_time)

    @protocol.tool(name="calendar.get_event", description="Read one calendar event by ID.")
    def calendar_get_event(event_id: str) -> Optional[dict[str, Any]]:
        return care.get_calendar_event(event_id)

    @protocol.tool(name="calendar.check_availability", description="Check whether a calendar time range is available.")
    def calendar_check_availability(start_time: str, end_time: str, exclude_event_id: Optional[str] = None) -> dict[str, Any]:
        return care.check_calendar_availability(start_time, end_time, exclude_event_id)

    @protocol.tool(name="calendar.update_event", description="Update a planned event. Requires exact active CARE plan/action binding.")
    def calendar_update_event(
        plan_id: str, action_hash: str, action_id: str, event_id: str,
        start_time: Optional[str] = None, end_time: Optional[str] = None,
        title: Optional[str] = None, simulate_failure: bool = False, dispatch_kind: str = "execute",
        dispatch_token: Optional[str] = None,
    ) -> dict[str, Any]:
        params = {"event_id": event_id}
        params.update({k: v for k, v in {"start_time": start_time, "end_time": end_time, "title": title}.items() if v is not None})
        return care.dispatch_tool("calendar.update_event", params, plan_id, action_hash, simulate_failure=simulate_failure, action_id=action_id, dispatch_token=dispatch_token, dispatch_kind=dispatch_kind)

    @protocol.tool(name="tickets.list_tickets", description="Read tickets, optionally filtered by status or escalation state.")
    def tickets_list_tickets(status: Optional[str] = None, is_escalated: Optional[bool] = None) -> list[dict[str, Any]]:
        return care.list_tickets(status, is_escalated)

    @protocol.tool(name="tickets.get_ticket", description="Read one ticket by ID.")
    def tickets_get_ticket(ticket_id: str) -> Optional[dict[str, Any]]:
        return care.get_ticket(ticket_id)

    @protocol.tool(name="tickets.update_status", description="Update a planned ticket. Requires exact active CARE plan/action binding.")
    def tickets_update_status(
        plan_id: str, action_hash: str, action_id: str, ticket_id: str,
        new_status: str, resolution_notes: Optional[str] = None, simulate_failure: bool = False,
        clear_resolution_notes: bool = False, dispatch_kind: str = "execute",
        dispatch_token: Optional[str] = None,
    ) -> dict[str, Any]:
        params = {"ticket_id": ticket_id, "new_status": new_status}
        if resolution_notes is not None:
            params["resolution_notes"] = resolution_notes
        if clear_resolution_notes:
            params["clear_resolution_notes"] = True
        return care.dispatch_tool("tickets.update_status", params, plan_id, action_hash, simulate_failure=simulate_failure, action_id=action_id, dispatch_token=dispatch_token, dispatch_kind=dispatch_kind)

    @protocol.tool(name="tickets.reopen_ticket", description="Reopen a planned ticket. Requires exact active CARE plan/action binding.")
    def tickets_reopen_ticket(
        plan_id: str, action_hash: str, action_id: str, ticket_id: str, reason: Optional[str] = None,
        simulate_failure: bool = False,
        dispatch_kind: str = "execute",
        dispatch_token: Optional[str] = None,
    ) -> dict[str, Any]:
        params = {"ticket_id": ticket_id}
        if reason is not None:
            params["reason"] = reason
        return care.dispatch_tool("tickets.reopen_ticket", params, plan_id, action_hash, simulate_failure=simulate_failure, action_id=action_id, dispatch_token=dispatch_token, dispatch_kind=dispatch_kind)

    @protocol.tool(name="care.get_tool_metadata", description="Read trusted server-side policy metadata for a registered operation.")
    def care_get_tool_metadata(operation: str) -> Optional[dict[str, Any]]:
        metadata = care.get_tool_metadata(operation)
        return metadata.model_dump() if metadata else None

    return protocol


def call_fastmcp_tool_sync(protocol: FastMCP, name: str, arguments: dict[str, Any]) -> Any:
    """Call a registered tool over FastMCP's in-memory MCP protocol transport."""
    from fastmcp import Client

    async def call() -> Any:
        try:
            async with Client(protocol) as client:
                result = await client.call_tool(name, arguments, raise_on_error=False)
        except Exception as exc:
            raise UnknownMCPOutcomeError(f"MCP response was not received for tool '{name}': {exc}") from exc
        else:
            if result.is_error:
                message = "; ".join(getattr(block, "text", "MCP tool failed") for block in result.content)
                raise RuntimeError(message)
            if result.structured_content is not None:
                return result.structured_content
            return result.content

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(call())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, call()).result()


if __name__ == "__main__":
    create_fastmcp_server().run()
