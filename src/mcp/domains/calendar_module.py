import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime
from src.schemas.tool_metadata import ToolMetadata

SEED_PATH = Path(__file__).resolve().parent.parent.parent.parent / "demo" / "seed_calendar.json"

CALENDAR_TOOL_METADATA: Dict[str, ToolMetadata] = {
    "calendar.list_events": ToolMetadata(
        operation="calendar.list_events",
        read_only=True,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=False,
    ),
    "calendar.get_event": ToolMetadata(
        operation="calendar.get_event",
        read_only=True,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=False,
    ),
    "calendar.check_availability": ToolMetadata(
        operation="calendar.check_availability",
        read_only=True,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=False,
    ),
    "calendar.update_event": ToolMetadata(
        operation="calendar.update_event",
        read_only=False,
        destructive=False,
        reversible=True,
        external_effect=False,
        affects_external_party=False,
        compensation_supported=True,
    ),
}

class CalendarDomainStore:
    def __init__(self, seed_file: Optional[Path | str] = None):
        self.seed_file = Path(seed_file or SEED_PATH)
        self.events: Dict[str, Dict[str, Any]] = {}
        self.reset()

    def reset(self) -> None:
        """Reloads store from seed json."""
        if self.seed_file.exists():
            with open(self.seed_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.events = {e["id"]: dict(e) for e in data}
        else:
            self.events = {}

    def list_events(self, start_time: Optional[str] = None, end_time: Optional[str] = None) -> List[Dict[str, Any]]:
        """List events optionally filtered by time overlap."""
        # For hackathon simplicity, returns all events or filtered if iso timestamps provided
        res = []
        for e in self.events.values():
            if start_time and end_time:
                # If event overlaps range
                e_start = e.get("start_time", "")
                e_end = e.get("end_time", "")
                if e_start <= end_time and e_end >= start_time:
                    res.append(dict(e))
            else:
                res.append(dict(e))
        return res

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        e = self.events.get(event_id)
        return dict(e) if e else None

    def check_availability(self, start_time: str, end_time: str, exclude_event_id: Optional[str] = None) -> Dict[str, Any]:
        """Check if time slot is occupied by any existing meeting."""
        for e in self.events.values():
            if exclude_event_id and e.get("id") == exclude_event_id:
                continue
            e_start = e.get("start_time", "")
            e_end = e.get("end_time", "")
            # check overlap
            if max(start_time, e_start) < min(end_time, e_end):
                return {
                    "available": False,
                    "conflicting_event": {
                        "id": e["id"],
                        "title": e["title"],
                        "start_time": e["start_time"],
                        "end_time": e["end_time"],
                    },
                }
        return {"available": True, "conflicting_event": None}

    def update_event(
        self,
        event_id: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        title: Optional[str] = None,
        plan_id: Optional[str] = None,
        action_hash: Optional[str] = None,
        simulate_failure: bool = False,
    ) -> Dict[str, Any]:
        """Gated state-changing tool to update calendar event timing."""
        if simulate_failure:
            raise RuntimeError(f"Simulated execution failure for tool calendar.update_event on {event_id}")

        if not plan_id or not action_hash:
            raise PermissionError("Direct unauthorized mutation rejected: plan_id and action_hash required.")

        if event_id not in self.events:
            raise KeyError(f"Calendar event {event_id} not found.")

        event = self.events[event_id]
        if start_time:
            event["start_time"] = start_time
        if end_time:
            event["end_time"] = end_time
        if title:
            event["title"] = title

        return dict(event)
