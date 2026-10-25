"""Transactional SQL appointment repository, with optimistic updates and audit."""
import json
import sqlite3
from uuid import uuid4

from voice_agent.identity import normalize_phone
from voice_agent.storage import utc_now

ACTIVE = ("confirmed", "modified")


class AppointmentRepository:
    def __init__(self, store, faults=None):
        self.store, self.faults = store, faults or {}

    def search_availability(self, date, service, customer_phone=""):
        if self.faults.get("availability") == "timeout":
            raise TimeoutError()
        if self.faults.get("availability") == "empty":
            return {"status": "success", "slots": []}
        phone = normalize_phone(customer_phone) if customer_phone else ""
        with self.store.connect() as db:
            taken = {r[0] for r in db.execute(
                "SELECT time FROM bookings WHERE date=? AND status IN ('confirmed','modified') AND customer_id<>?", (date, phone))}
        return {"status": "success", "slots": [t for t in ("09:00", "10:00", "14:00", "15:00") if t not in taken]}

    def find_booking(self, customer_phone):
        phone = normalize_phone(customer_phone)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM bookings WHERE customer_id=? AND status IN ('confirmed','modified')", (phone,)).fetchone()
        return dict(row) if row else {"status": "not_found"}

    def get_booking(self, call_id, idempotency_key):
        if self.faults.get("get_booking") == "timeout":
            raise TimeoutError()
        with self.store.connect() as db:
            row = db.execute("SELECT result FROM booking_mutations WHERE request_key=? AND call_id=?", (idempotency_key, call_id)).fetchone()
        return json.loads(row[0]) if row else {"status": "not_found"}

    def mutate_booking(self, call_id, customer_phone, action, idempotency_key,
                       patient_name="", service="", date="", time="", expected_revision=0):
        phone = normalize_phone(customer_phone)
        if action not in {"create", "modify", "cancel"}:
            raise ValueError("Invalid appointment action")
        if action != "cancel" and (not patient_name or service not in {"cleaning", "consultation"} or time not in {"09:00", "10:00", "14:00", "15:00"}):
            raise ValueError("Invalid appointment fields")
        if self.faults.get("create_booking") == "timeout_before_commit":
            raise TimeoutError()
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                from voice_agent.execution import assert_live
                assert_live(db)
                prior = db.execute("SELECT result FROM booking_mutations WHERE request_key=? AND call_id=?", (idempotency_key, call_id)).fetchone()
                if prior:
                    return json.loads(prior[0])
                row = db.execute("SELECT * FROM bookings WHERE customer_id=? AND status IN ('confirmed','modified')", (phone,)).fetchone()
                before = dict(row) if row else None
                if action == "create":
                    if row:
                        return {**before, "status": "already_exists"}
                    booking_id = "BK-" + uuid4().hex[:10].upper()
                    db.execute("INSERT INTO bookings (id,call_id,idempotency_key,customer_id,patient_name,service,date,time,status,revision,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (booking_id, call_id, idempotency_key, phone, patient_name, service, date, time, "confirmed", 1, utc_now()))
                else:
                    if not row:
                        return {"status": "not_found"}
                    if row["revision"] != expected_revision:
                        return {"status": "stale", "current_revision": row["revision"]}
                    booking_id = row["id"]
                    if action == "cancel":
                        db.execute("UPDATE bookings SET status='cancel',revision=revision+1,call_id=?,updated_at=? WHERE id=?", (call_id, utc_now(), booking_id))
                    else:
                        db.execute("UPDATE bookings SET patient_name=?,service=?,date=?,time=?,status='modified',revision=revision+1,call_id=?,updated_at=? WHERE id=?",
                                   (patient_name, service, date, time, call_id, utc_now(), booking_id))
                after = dict(db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
                db.execute("INSERT INTO booking_mutations VALUES (?,?,?,?,?,?,?)", (idempotency_key, call_id, booking_id, action, json.dumps(before), json.dumps(after), utc_now()))
        except sqlite3.IntegrityError:
            return {"status": "conflict"}
        if self.faults.get("create_booking") == "timeout_after_commit":
            raise TimeoutError()
        return after

    def create_booking(self, call_id, customer_phone, patient_name, service, date, time, idempotency_key, expected_revision=0):
        return self.mutate_booking(call_id, customer_phone, "create", idempotency_key, patient_name, service, date, time, expected_revision)
