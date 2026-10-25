from uuid import uuid4

from langchain_core.tools import tool

from voice_agent.appointments import AppointmentRepository
from voice_agent.insurance_rag import InsuranceRAG


class LocalBackend(AppointmentRepository):
    """SQLite is the appointment system. Only insurance evidence is a demo fixture."""
    def __init__(self, store, settings, faults=None):
        super().__init__(store, faults)
        self.insurance = InsuranceRAG(settings, store)

    def check_insurance(self, call_id, payer, member_id, service=""):
        if self.faults.get("insurance") == "timeout":
            raise TimeoutError()
        return self.insurance.check(call_id, payer, member_id, service)

    def transfer_call(self, call_id, summary, idempotency_key):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from voice_agent.execution import assert_live
            assert_live(db)
            prior = db.execute("SELECT * FROM transfers WHERE idempotency_key=? AND call_id=?", (idempotency_key, call_id)).fetchone()
            if prior:
                return dict(prior)
            result = {"id": "HE-" + uuid4().hex[:10].upper(), "status": "human_escalation", "summary": summary}
            db.execute("INSERT INTO transfers VALUES (?,?,?,?,?)", (result["id"], call_id, idempotency_key, result["status"], summary))
        return result


def make_tools(backend):
    @tool
    def search_availability(date: str, service: str, customer_phone: str = "") -> dict:
        """Read free local SQL slots, excluding this customer's current booking."""
        return backend.search_availability(date, service, customer_phone)

    @tool
    def find_booking(customer_phone: str) -> dict:
        """Read the active local appointment by normalized customer phone."""
        return backend.find_booking(customer_phone)

    @tool
    def create_booking(call_id: str, customer_phone: str, patient_name: str, service: str, date: str, time: str, idempotency_key: str, expected_revision: int = 0) -> dict:
        """Insert a local appointment only after code verifies explicit confirmation."""
        return backend.create_booking(call_id, customer_phone, patient_name, service, date, time, idempotency_key, expected_revision)

    @tool
    def mutate_booking(call_id: str, customer_phone: str, action: str, idempotency_key: str, patient_name: str = "", service: str = "", date: str = "", time: str = "", expected_revision: int = 0) -> dict:
        """Modify or cancel an existing appointment with a confirmed revision."""
        return backend.mutate_booking(call_id, customer_phone, action, idempotency_key, patient_name, service, date, time, expected_revision)

    @tool
    def get_booking(call_id: str, idempotency_key: str) -> dict:
        """Read a committed mutation receipt to reconcile a lost response."""
        return backend.get_booking(call_id, idempotency_key)

    @tool
    def check_insurance(call_id: str, payer: str, member_id: str, service: str = "") -> dict:
        """Retrieve insurance evidence and assess it with one structured LLM call in Claude mode."""
        return backend.check_insurance(call_id, payer, member_id, service)

    @tool
    def transfer_call(call_id: str, summary: str, idempotency_key: str) -> dict:
        """Record human_escalation locally; never claim a real phone connection."""
        return backend.transfer_call(call_id, summary, idempotency_key)

    return {t.name: t for t in (search_availability, find_booking, create_booking, mutate_booking, get_booking, check_insurance, transfer_call)}
