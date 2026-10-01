import json
import hashlib
from typing import Any, List, Union
from src.schemas.plan import PlannedAction

def compute_action_hash(actions: List[Union[PlannedAction, dict[str, Any]]]) -> str:
    """
    Computes canonical SHA-256 hash over an actions list.
    Enforces canonical JSON: sorted keys, compact separators (',', ':'),
    and strips volatile runtime fields (status, result, error).
    """
    cleaned = []
    for act in actions:
        if hasattr(act, "model_dump"):
            data = act.model_dump()
        elif isinstance(act, dict):
            data = act.copy()
        elif hasattr(act, "dict"):
            data = act.dict()
        else:
            raise TypeError(f"Unsupported action type: {type(act)}")
        
        # Strip mutable runtime fields
        data.pop("status", None)
        data.pop("result", None)
        data.pop("error", None)
        cleaned.append(data)

    canonical = json.dumps(cleaned, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
