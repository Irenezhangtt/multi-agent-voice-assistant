import pytest
from evals.business_metrics import aggregate


def test_metrics_use_booking_intent_denominator_and_do_not_invent_hangups():
    rows=[{'call_id':str(i),'target_action':action,'new_booking_verified':booked,
           'business_success':success,'human_escalation':human,'expected_escalation':required,
           'simulation_duration_ms':1000} for i,(action,booked,success,human,required) in enumerate([
               ('create',True,True,False,False),('create',False,False,False,False),
               ('cancel',False,True,False,False),(None,False,True,True,True),
               (None,False,True,False,False)])]
    metrics=aggregate(rows)
    assert metrics['booking_success']=={'numerator':1,'denominator':2,'rate':0.5}
    assert metrics['human_escalation_marker_rate']['rate']==0.2
    assert metrics['required_escalation_success']['rate']==1
    assert metrics['user_hangup_rate'] is None
    assert metrics['mean_phone_call_duration_seconds'] is None
    assert metrics['mean_simulation_duration_seconds']==1
    with pytest.raises(ValueError,match='Duplicate'):
        aggregate(rows+[rows[0]])


def test_zero_denominators_are_unavailable_not_zero():
    metrics=aggregate([])
    assert metrics['booking_success']['rate'] is None
    assert metrics['required_escalation_success']['rate'] is None
