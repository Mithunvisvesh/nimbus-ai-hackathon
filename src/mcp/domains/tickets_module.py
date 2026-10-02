import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.schemas.tool_metadata import ToolMetadata

SEED_PATH = Path(__file__).resolve().parent.parent.parent.parent / "demo" / "seed_tickets.json"

TICKETS_TOOL_METADATA: Dict[str, ToolMetadata] = {
    "tickets.list_tickets": ToolMetadata(
        operation="tickets.list_tickets",
        read_only=True,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=False,
    ),
    "tickets.get_ticket": ToolMetadata(
        operation="tickets.get_ticket",
        read_only=True,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=False,
    ),
    "tickets.update_status": ToolMetadata(
        operation="tickets.update_status",
        read_only=False,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=True,
    ),
    "tickets.reopen_ticket": ToolMetadata(
        operation="tickets.reopen_ticket",
        read_only=False,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=True,
    ),
    "tickets.create_ticket": ToolMetadata(
        operation="tickets.create_ticket",
        read_only=False,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=True,
    ),
    "tickets.delete_ticket": ToolMetadata(
        operation="tickets.delete_ticket",
        read_only=False,
        destructive=True,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=True,
    ),
    "tickets.update_ticket": ToolMetadata(
        operation="tickets.update_ticket",
        read_only=False,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=True,
    ),
}

class TicketsDomainStore:
    def __init__(self, seed_file: Optional[Path | str] = None):
        self.seed_file = Path(seed_file or SEED_PATH)
        self.tickets: Dict[str, Dict[str, Any]] = {}
        self._authorization_token: object | None = None
        self.reset()

    def _bind_authorization_token(self, token: object) -> None:
        self._authorization_token = token

    def reset(self) -> None:
        """Reloads store from seed json."""
        if self.seed_file.exists():
            with open(self.seed_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.tickets = {t["id"]: dict(t) for t in data}
        else:
            self.tickets = {}

    def list_tickets(
        self,
        status: Optional[str] = None,
        is_escalated: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """List tickets optionally filtered by status or escalation flag."""
        res = []
        for t in self.tickets.values():
            if status is not None and t.get("status") != status:
                continue
            if is_escalated is not None and t.get("is_escalated") != is_escalated:
                continue
            res.append(dict(t))
        return res

    def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        t = self.tickets.get(ticket_id)
        return dict(t) if t else None

    def update_status(
        self,
        ticket_id: str,
        new_status: str,
        resolution_notes: Optional[str] = None,
        clear_resolution_notes: bool = False,
        plan_id: Optional[str] = None,
        action_hash: Optional[str] = None,
        simulate_failure: bool = False,
        _authorization_token: object | None = None,
    ) -> Dict[str, Any]:
        """Gated state-changing tool to update ticket status."""
        if simulate_failure:
            raise RuntimeError(f"Simulated execution failure for tool tickets.update_status on {ticket_id}")

        if _authorization_token is None or _authorization_token is not self._authorization_token:
            raise PermissionError("Ticket mutation must be authorized by the CARE MCP server.")
        if not plan_id or not action_hash:
            raise PermissionError("Direct unauthorized mutation rejected: plan_id and action_hash required.")

        if ticket_id not in self.tickets:
            raise KeyError(f"Ticket {ticket_id} not found.")

        t = self.tickets[ticket_id]
        t["status"] = new_status
        if clear_resolution_notes:
            t.pop("resolution_notes", None)
        elif resolution_notes is not None:
            t["resolution_notes"] = resolution_notes

        return dict(t)

    def reopen_ticket(
        self,
        ticket_id: str,
        reason: Optional[str] = None,
        plan_id: Optional[str] = None,
        action_hash: Optional[str] = None,
        simulate_failure: bool = False,
        _authorization_token: object | None = None,
    ) -> Dict[str, Any]:
        """Gated state-changing tool to reopen a ticket."""
        return self.update_status(
            ticket_id=ticket_id,
            new_status="open",
            resolution_notes=reason,
            clear_resolution_notes=False,
            plan_id=plan_id,
            action_hash=action_hash,
            simulate_failure=simulate_failure,
            _authorization_token=_authorization_token,
        )

    def create_ticket(
        self,
        ticket_id: str,
        title: str,
        priority: str = "medium",
        assigned_to: str = "user_mithun",
        tags: Optional[List[str]] = None,
        is_escalated: bool = False,
        status: str = "open",
        plan_id: Optional[str] = None,
        action_hash: Optional[str] = None,
        simulate_failure: bool = False,
        _authorization_token: object | None = None,
    ) -> Dict[str, Any]:
        """Gated state-changing tool to create a ticket."""
        if simulate_failure:
            raise RuntimeError(f"Simulated execution failure for tool tickets.create_ticket on {ticket_id}")

        if _authorization_token is None or _authorization_token is not self._authorization_token:
            raise PermissionError("Ticket mutation must be authorized by the CARE MCP server.")
        if not plan_id or not action_hash:
            raise PermissionError("Direct unauthorized mutation rejected: plan_id and action_hash required.")

        new_ticket = {
            "id": ticket_id,
            "title": title,
            "status": status,
            "priority": priority,
            "assigned_to": assigned_to,
            "tags": tags or [],
            "is_escalated": is_escalated,
        }
        self.tickets[ticket_id] = new_ticket
        return dict(new_ticket)

    def delete_ticket(
        self,
        ticket_id: str,
        plan_id: Optional[str] = None,
        action_hash: Optional[str] = None,
        simulate_failure: bool = False,
        _authorization_token: object | None = None,
    ) -> Dict[str, Any]:
        """Gated state-changing tool to delete a ticket."""
        if simulate_failure:
            raise RuntimeError(f"Simulated execution failure for tool tickets.delete_ticket on {ticket_id}")

        if _authorization_token is None or _authorization_token is not self._authorization_token:
            raise PermissionError("Ticket mutation must be authorized by the CARE MCP server.")
        if not plan_id or not action_hash:
            raise PermissionError("Direct unauthorized mutation rejected: plan_id and action_hash required.")

        if ticket_id not in self.tickets:
            raise KeyError(f"Ticket {ticket_id} not found.")

        deleted = self.tickets.pop(ticket_id)
        return dict(deleted)

    def update_ticket(
        self,
        ticket_id: str,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
        tags: Optional[List[str]] = None,
        title: Optional[str] = None,
        plan_id: Optional[str] = None,
        action_hash: Optional[str] = None,
        simulate_failure: bool = False,
        _authorization_token: object | None = None,
    ) -> Dict[str, Any]:
        """Gated state-changing tool to update ticket attributes."""
        if simulate_failure:
            raise RuntimeError(f"Simulated execution failure for tool tickets.update_ticket on {ticket_id}")

        if _authorization_token is None or _authorization_token is not self._authorization_token:
            raise PermissionError("Ticket mutation must be authorized by the CARE MCP server.")
        if not plan_id or not action_hash:
            raise PermissionError("Direct unauthorized mutation rejected: plan_id and action_hash required.")

        if ticket_id not in self.tickets:
            raise KeyError(f"Ticket {ticket_id} not found.")

        t = self.tickets[ticket_id]
        if priority is not None:
            t["priority"] = priority
        if assigned_to is not None:
            t["assigned_to"] = assigned_to
        if tags is not None:
            t["tags"] = tags
        if title is not None:
            t["title"] = title
        return dict(t)
