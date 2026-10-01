import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.schemas.plan import CandidatePlan, PlanStatus
from src.schemas.journal import JournalRecord, ActionJournalStatus

from contextlib import contextmanager

DB_PATH_DEFAULT = Path(__file__).resolve().parent.parent.parent / "care_journal.db"

class ActionJournalDB:
    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = str(db_path or DB_PATH_DEFAULT)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS approved_plans (
                plan_id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                user_role TEXT NOT NULL,
                policy_outcome TEXT NOT NULL,
                action_hash TEXT NOT NULL,
                actions_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                executing_at TEXT,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS journal_records (
                record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                action_hash TEXT NOT NULL,
                actor TEXT NOT NULL,
                user_role TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                before_state TEXT NOT NULL,
                after_state TEXT,
                compensation_action TEXT,
                status TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                result TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS drift_incidents (
                incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                expected_after_state TEXT NOT NULL,
                observed_drift_state TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                remediation_status TEXT DEFAULT 'pending_human_review',
                notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_approved_plans_status ON approved_plans(status);
            CREATE INDEX IF NOT EXISTS idx_journal_plan_id ON journal_records(plan_id);
            CREATE INDEX IF NOT EXISTS idx_journal_resource ON journal_records(resource_type, resource_id);
            CREATE INDEX IF NOT EXISTS idx_journal_status ON journal_records(status);
            """)

    def save_approved_plan(self, plan: CandidatePlan) -> None:
        """Stores an approved candidate plan for cryptographic binding and replay control."""
        now = datetime.now(timezone.utc).isoformat()
        actions_data = [act.model_dump() for act in plan.actions]
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO approved_plans 
                (plan_id, actor, user_role, policy_outcome, action_hash, actions_json, status, created_at, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.actor,
                    plan.user_role,
                    plan.policy_outcome.value if plan.policy_outcome else "approved",
                    plan.action_hash or "",
                    json.dumps(actions_data, sort_keys=True),
                    PlanStatus.APPROVED.value,
                    now,
                    now,
                ),
            )

    def transition_plan_to_executing(self, plan_id: str) -> bool:
        """
        Atomic Compare-And-Set: transitions status from 'approved' to 'executing'.
        Returns True if exactly one row transitioned, False if already consumed or invalid.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE approved_plans
                SET status = ?, executing_at = ?
                WHERE plan_id = ? AND status = ?
                """,
                (PlanStatus.EXECUTING.value, now, plan_id, PlanStatus.APPROVED.value),
            )
            return cur.rowcount == 1

    def update_plan_status(self, plan_id: str, new_status: PlanStatus) -> None:
        now = datetime.now(timezone.utc).isoformat()
        completed_at = now if new_status in (PlanStatus.DONE, PlanStatus.FAILED, PlanStatus.COMPENSATED) else None
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE approved_plans
                SET status = ?, completed_at = COALESCE(?, completed_at)
                WHERE plan_id = ?
                """,
                (new_status.value, completed_at, plan_id),
            )

    def get_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM approved_plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            res["actions"] = json.loads(res["actions_json"])
            return res

    def log_pre_action(
        self,
        plan_id: str,
        action_id: str,
        action_hash: str,
        actor: str,
        user_role: str,
        resource_type: str,
        resource_id: str,
        operation: str,
        before_state: Dict[str, Any],
        compensation_action: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Writes pre-action intent with status='executing' before tool invocation."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO journal_records
                (plan_id, action_id, action_hash, actor, user_role, resource_type, resource_id,
                 operation, before_state, compensation_action, status, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    action_id,
                    action_hash,
                    actor,
                    user_role,
                    resource_type,
                    resource_id,
                    operation,
                    json.dumps(before_state, sort_keys=True),
                    json.dumps(compensation_action, sort_keys=True) if compensation_action else None,
                    ActionJournalStatus.EXECUTING.value,
                    now,
                ),
            )
            return cur.lastrowid

    def log_post_action_success(
        self,
        record_id: int,
        after_state: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Updates the write-ahead journal entry with after_state and status='done' upon return."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE journal_records
                SET after_state = ?, result = ?, status = ?
                WHERE record_id = ?
                """,
                (
                    json.dumps(after_state, sort_keys=True),
                    json.dumps(result, sort_keys=True) if result else None,
                    ActionJournalStatus.DONE.value,
                    record_id,
                ),
            )

    def log_post_action_failure(
        self,
        record_id: int,
        error_msg: str,
    ) -> None:
        """Updates the journal entry with status='failed' and error details."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE journal_records
                SET error = ?, status = ?
                WHERE record_id = ?
                """,
                (error_msg, ActionJournalStatus.FAILED.value, record_id),
            )

    def get_journal_records(self, plan_id: str) -> List[JournalRecord]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_records WHERE plan_id = ? ORDER BY record_id ASC",
                (plan_id,),
            ).fetchall()
            records = []
            for r in rows:
                records.append(
                    JournalRecord(
                        plan_id=r["plan_id"],
                        action_id=r["action_id"],
                        action_hash=r["action_hash"],
                        actor=r["actor"],
                        user_role=r["user_role"],
                        resource_type=r["resource_type"],
                        resource_id=r["resource_id"],
                        operation=r["operation"],
                        before_state=json.loads(r["before_state"]) if r["before_state"] else {},
                        after_state=json.loads(r["after_state"]) if r["after_state"] else None,
                        compensation_action=json.loads(r["compensation_action"]) if r["compensation_action"] else None,
                        status=ActionJournalStatus(r["status"]),
                        timestamp=r["timestamp"],
                        result=json.loads(r["result"]) if r["result"] else None,
                        error=r["error"],
                    )
                )
            return records

    def update_action_status(self, record_id: int, new_status: ActionJournalStatus) -> None:
        """Updates the status of a specific journal action record."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE journal_records SET status = ? WHERE record_id = ?",
                (new_status.value, record_id),
            )

    def log_drift_incident(
        self,
        plan_id: str,
        action_id: str,
        resource_id: str,
        expected_after_state: Dict[str, Any],
        observed_drift_state: Dict[str, Any],
        notes: Optional[str] = None,
    ) -> int:
        """Persists a detected drift incident in the drift_incidents table."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO drift_incidents
                (plan_id, action_id, resource_id, expected_after_state, observed_drift_state, detected_at, remediation_status, notes)
                VALUES (?, ?, ?, ?, ?, ?, 'pending_human_review', ?)
                """,
                (
                    plan_id,
                    action_id,
                    resource_id,
                    json.dumps(expected_after_state, sort_keys=True),
                    json.dumps(observed_drift_state, sort_keys=True),
                    now,
                    notes,
                ),
            )
            return cur.lastrowid

    def get_drift_incidents(self, plan_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves drift incident records."""
        with self._get_connection() as conn:
            if plan_id:
                rows = conn.execute(
                    "SELECT * FROM drift_incidents WHERE plan_id = ? ORDER BY incident_id DESC",
                    (plan_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM drift_incidents ORDER BY incident_id DESC"
                ).fetchall()

            res = []
            for r in rows:
                item = dict(r)
                item["expected_after_state"] = json.loads(item["expected_after_state"])
                item["observed_drift_state"] = json.loads(item["observed_drift_state"])
                res.append(item)
            return res
