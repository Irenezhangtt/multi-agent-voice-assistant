from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Local synthetic-data store; each transaction uses its own connection."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    state TEXT NOT NULL, versions TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    call_id TEXT NOT NULL, request_id TEXT NOT NULL, message TEXT NOT NULL,
                    response TEXT NOT NULL, PRIMARY KEY (call_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS traces (
                    id TEXT PRIMARY KEY, call_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id TEXT PRIMARY KEY, call_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL, summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS insurance_queries (
                    id TEXT PRIMARY KEY, call_id TEXT NOT NULL, status TEXT NOT NULL, payer TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_effects(effect_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS insurance_retrievals(query_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
            """)

        self.migrate_bookings()

    def migrate_bookings(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(bookings)")}
            if columns and "customer_id" not in columns:
                db.execute("ALTER TABLE bookings RENAME TO bookings_legacy_v1")
            db.execute("""CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY, call_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
                customer_id TEXT NOT NULL, patient_name TEXT NOT NULL, service TEXT NOT NULL,
                date TEXT NOT NULL, time TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('confirmed','modified','cancel')),
                revision INTEGER NOT NULL, updated_at TEXT NOT NULL)""")
            if columns and "customer_id" not in columns:
                # Keep historical rows; never guess a real customer's phone from a name.
                db.execute("""INSERT INTO bookings SELECT id,call_id,idempotency_key,
                    'legacy:' || id,patient_name,service,date,time,
                    CASE WHEN status='cancel' THEN 'cancel' ELSE 'confirmed' END,1,?
                    FROM bookings_legacy_v1""", (utc_now(),))
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_customer ON bookings(customer_id) WHERE status IN ('confirmed','modified')")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_slot ON bookings(date,time) WHERE status IN ('confirmed','modified')")
            db.execute("""CREATE TABLE IF NOT EXISTS booking_mutations (
                request_key TEXT PRIMARY KEY,call_id TEXT NOT NULL,booking_id TEXT NOT NULL,
                action TEXT NOT NULL,before_json TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS insurance_assessments (
                id TEXT PRIMARY KEY,call_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_call(self, versions: dict):
        call_id, case_id = str(uuid4()), str(uuid4())
        state = {
            "call_id": call_id, "case_id": case_id, "active_workflow": "",
            "pending_workflows": [], "status": "ready", "booking": {}, "insurance": {},
            "handoff": {}, "history": [], "events": [],
            "reply": "Hello. I can manage local appointments, check demo insurance rules, or record a human escalation. Please use fictional information.",
        }
        with self.connect() as db:
            db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?)", (
                call_id, case_id, utc_now(), json.dumps(state, ensure_ascii=False), json.dumps(versions)
            ))
        return state

    def get_call(self, call_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            raise KeyError(call_id)
        return {"state": json.loads(row["state"]), "versions": json.loads(row["versions"])}

    def save_call(self, call_id: str, state: dict):
        with self.connect() as db:
            db.execute("UPDATE calls SET state=? WHERE id=?", (json.dumps(state, ensure_ascii=False), call_id))

    def save_progress(self, state, execution):
        """Persist the exact verified offer before it may be spoken during a turn."""
        from voice_agent.execution import assert_live
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            assert_live(db, execution.scope())
            db.execute('UPDATE calls SET state=? WHERE id=?',
                       (json.dumps(state, ensure_ascii=False), execution.row['call_id']))
            db.execute('INSERT INTO execution_steps(execution_id,step_key,payload) VALUES (?,?,?) '
                       'ON CONFLICT(execution_id,step_key) DO UPDATE SET payload=excluded.payload',
                       (execution.row['id'],'published:reply',json.dumps(state,ensure_ascii=False)))

    def get_turn(self, call_id: str, request_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM turns WHERE call_id=? AND request_id=?", (call_id, request_id)).fetchone()
        return dict(row) if row else None

    def save_turn(self, call_id: str, request_id: str, message: str, response: dict, trace: dict, execution=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if execution:
                from voice_agent.execution import assert_live
                assert_live(db, execution.scope())
            db.execute("INSERT INTO turns VALUES (?, ?, ?, ?)", (
                call_id, request_id, message, json.dumps(response, ensure_ascii=False)
            ))
            db.execute("INSERT INTO traces VALUES (?, ?, ?, ?)", (
                trace["trace_id"], call_id, utc_now(), json.dumps(trace, ensure_ascii=False)
            ))
            db.execute("UPDATE calls SET state=? WHERE id=?", (
                json.dumps(response["state"], ensure_ascii=False), call_id
            ))
            if execution:
                db.execute("UPDATE executions SET status='completed' WHERE id=?", (execution.row["id"],))

    def traces(self, call_id: str):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM traces WHERE call_id=? ORDER BY rowid", (call_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def voice_event(self, call_id: str, payload: dict):
        with self.connect() as db:
            db.execute("INSERT INTO voice_events(call_id, created_at, payload) VALUES (?, ?, ?)",
                       (call_id, utc_now(), json.dumps(payload, ensure_ascii=False)))

    def voice_events(self, call_id: str):
        with self.connect() as db:
            rows = db.execute("SELECT created_at,payload FROM voice_events WHERE call_id=? ORDER BY id DESC LIMIT 500", (call_id,)).fetchall()
        return [{"created_at": row["created_at"], **json.loads(row["payload"])} for row in reversed(rows)]

    def outcomes(self, call_id: str):
        with self.connect() as db:
            return {
                table: [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE call_id=?", (call_id,))]
                for table in ("bookings", "transfers", "insurance_queries")
            }
