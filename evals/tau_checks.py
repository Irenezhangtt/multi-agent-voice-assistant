"""Independent, read-only SQL assertions for the project-authored τ clinic domain."""
import json
from pathlib import Path
import sqlite3

from voice_agent.router import explicit_confirmation

ALLOWED = {'booking_count', 'mutation_actions', 'handoff_count', 'booking', 'preserve_seed_id',
           'insurance_status', 'insurance_citations', 'pending_booking_status'}


def grade_sql(database, call_id, expected, seed_id=None):
    unknown = set(expected) - ALLOWED
    if unknown:
        raise ValueError('Unknown clinic assertion keys: ' + ','.join(sorted(unknown)))
    checks = []
    def check(name, passed, actual):
        checks.append({'name': name, 'passed': bool(passed), 'actual': actual})
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        bookings = [dict(r) for r in db.execute('SELECT * FROM bookings ORDER BY rowid')]
        mutations = [dict(r) for r in db.execute('SELECT * FROM booking_mutations ORDER BY rowid')]
        transfers = [dict(r) for r in db.execute('SELECT * FROM transfers ORDER BY rowid')]
        assessments = [json.loads(r[0]) for r in db.execute('SELECT payload FROM insurance_assessments ORDER BY rowid')]
        turns = [dict(r) for r in db.execute('SELECT * FROM turns WHERE call_id=? ORDER BY rowid', (call_id,))]
        call = db.execute('SELECT state,versions FROM calls WHERE id=?', (call_id,)).fetchone()
        # A verified offer may have been durably published while a sibling task
        # was still running, then superseded by the caller's confirmation.
        bases = {r['request_id']:json.loads(r['base']) for r in db.execute('SELECT request_id,base FROM executions WHERE call_id=?',(call_id,))}
    if not call:
        raise ValueError('Missing trial call')
    state, versions = json.loads(call['state']), json.loads(call['versions'])
    check('real_multi_agent_architecture', versions.get('architecture') == 'multi-agent' and versions.get('mode') == 'claude', versions)
    check('conversation_has_committed_turn', bool(turns), len(turns))
    check('booking_count', len(bookings) == expected['booking_count'], len(bookings))
    check('mutation_actions', [m['action'] for m in mutations] == expected['mutation_actions'], [m['action'] for m in mutations])
    check('handoff_count', len(transfers) == expected['handoff_count'], len(transfers))
    check('handoff_markers', all(t['status'] == 'human_escalation' for t in transfers), [t['status'] for t in transfers])
    if expected.get('booking'):
        target = {('customer_id' if k == 'customer_phone' else k): v for k, v in expected['booking'].items()}
        check('appointment_fields', len(bookings) == 1 and all(bookings[0].get(k) == v for k, v in target.items()), bookings)
    if expected.get('preserve_seed_id'):
        check('original_row_preserved', bool(seed_id) and len(bookings) == 1 and bookings[0]['id'] == seed_id, [b['id'] for b in bookings])
    if 'insurance_status' in expected:
        latest = assessments[-1] if assessments else {}
        check('insurance_status_in_sql', latest.get('status') == expected['insurance_status'], latest.get('status'))
        check('demo_evidence_provenance', latest.get('demo_only') is True and latest.get('source') == 'local-rag' and bool(latest.get('kb_sha256')), {k: latest.get(k) for k in ('source', 'demo_only', 'kb_sha256')})
        if expected.get('insurance_citations'):
            check('insurance_supporting_citations', set(expected['insurance_citations']) <= set(latest.get('citations', [])), latest.get('citations'))
    else:
        check('no_unrequested_insurance_write', not assessments, len(assessments))
    if 'pending_booking_status' in expected:
        check('review_without_consent', state.get('booking', {}).get('status') == expected['pending_booking_status'], state.get('booking', {}).get('status'))

    # Match SQL mutation receipts against real saved turns, never an agent's success sentence.
    verified_commits = set()
    escalated, after_escalation = False, []
    responses = [json.loads(t['response']) for t in turns]
    for index, (turn, response) in enumerate(zip(turns, responses)):
        for event in response['state'].get('events', []):
            if event.get('type') != 'mcp_tool':
                continue
            if escalated:
                after_escalation.append(event['node'])
            if event.get('node') == 'mark_human_escalation' and event.get('status') == 'ok' and not event.get('result', {}).get('error'):
                escalated = True
            if event.get('node') != 'commit_appointment' or event.get('status') != 'ok':
                continue
            receipt = event.get('result', {}).get('patch', {}).get('booking', {})
            previous = bases.get(turn['request_id'], responses[index-1]['state'] if index else {})
            review = previous.get('booking', {}).get('review_summary')
            consent = explicit_confirmation(turn['message']) and bool(review) and review in previous.get('reply', '')
            check('explicit_previous_turn_consent:' + turn['request_id'], consent, turn['message'])
            if consent and receipt.get('id'):
                verified_commits.add((receipt['id'], receipt.get('revision')))
    uncovered = []
    for mutation in mutations:
        if mutation['request_key'] == 'tau-seed':
            continue
        result = json.loads(mutation['result'])
        if (result.get('id'), result.get('revision')) not in verified_commits:
            uncovered.append(mutation['request_key'])
    check('every_sql_mutation_has_confirmed_mcp_receipt', not uncovered, uncovered)
    check('no_tools_after_human_escalation', not after_escalation, after_escalation)
    return {'passed': all(c['passed'] for c in checks), 'checks': checks,
            'outcomes': {'bookings': bookings, 'transfers': transfers, 'insurance_assessments': assessments}}
