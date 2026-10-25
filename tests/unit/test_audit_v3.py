"""Independent adversarial corpus: no network, no model calls."""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from odin.contracts import ConstraintEvaluation, ConstraintGroup
from odin.contracts.api import StructuredForm
from odin.core.constraints import combine_and, combine_or, rank_class
from odin.core.tree import evaluate_tree
from odin.llm.free_text import InterpretationDelta, apply_delta

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def apply(payload, text, base=None):
    return apply_delta(InterpretationDelta.model_validate(payload), text=text,
                       base=base or StructuredForm(), reference_datetime=NOW)


@pytest.mark.parametrize('field,span,base_kwargs,expected', [
    ('budget','Бюджет до 40000',{'budget_amount':'40000','budget_currency':'RUB','budget_required':True},'40000'),
    ('transport','Только поездом',{'transport_modes':['rail'],'transport_modes_required':True},['rail']),
    ('breakfast','Завтрак обязателен',{'breakfast':True,'breakfast_required':True},True),
    ('stars_min','Только 4 звезды',{'stars_min':4,'stars_required':True},4),
    ('pets','С собакой обязательно',{'pets':[{'kind':'dog','count':1}],'pets_required':True},1),
    ('children_ages','Дети 3 и 14 лет',{'children_ages':[3,14]},[3,14]),
    ('return_trip','Туда и обратно',{'return_trip':True},True),
])
def test_clear_without_revocation_is_rejected(field,span,base_kwargs,expected):
    form, report=apply({'clear':[{'field':field,'span':span}]},span,StructuredForm.model_validate(base_kwargs))
    attr={'budget':'budget_amount','transport':'transport_modes','stars_min':'stars_min','children_ages':'children_ages','return_trip':'return_trip','pets':'pets','breakfast':'breakfast'}[field]
    got=getattr(form,attr)
    if field=='budget':
        assert str(got)==expected
    elif field=='pets':
        assert len(got)==expected
    else:
        assert got==expected
    assert f'clear:{field}' in report.rejected


@pytest.mark.parametrize('field,span,base_kwargs,attr,expected', [
    ('budget','Бюджет больше не важен',{'budget_amount':'40000','budget_currency':'RUB','budget_required':True},'budget_amount',None),
    ('breakfast','Завтрак больше не обязателен',{'breakfast':True,'breakfast_required':True},'breakfast',None),
    ('children_ages','Теперь без детей',{'children_ages':[3,14]},'children_ages',[]),
    ('transport','Можно и самолётом',{'transport_modes':['rail'],'transport_modes_required':True},'transport_modes',[]),
])
def test_clear_explicit_revocation(field,span,base_kwargs,attr,expected):
    form,report=apply({'clear':[{'field':field,'span':span}]},span,StructuredForm.model_validate(base_kwargs))
    assert getattr(form,attr)==expected
    assert f'clear:{field}' in report.accepted


def ev(cid,status):
    return ConstraintEvaluation(constraint_id=cid,component_id='hotel',plan_id=None,status=status,
      unknown_reason='data_missing' if status=='unknown' else None,evidence_refs=[],reason='audit',deviation=None)


def grp(gid,op,children):
    return ConstraintGroup(group_id=gid,operator=op,children=children,scope='hotel',activation=None,raw_text=None)


@pytest.mark.parametrize('a,b,expected_and,expected_or', [
 ('matched','matched','matched','matched'),('matched','unknown','unknown','matched'),
 ('matched','violated','violated','matched'),('unknown','unknown','unknown','unknown'),
 ('unknown','violated','violated','unknown'),('violated','violated','violated','violated'),
 ('not_applicable','matched','matched','matched'),('not_applicable','unknown','unknown','unknown'),
 ('not_applicable','violated','violated','violated'),('not_applicable','not_applicable','not_applicable','not_applicable'),
])
def test_three_valued_logic(a,b,expected_and,expected_or):
    assert combine_and([a,b])==expected_and
    assert combine_or([a,b])==expected_or


@pytest.mark.parametrize('stars,breakfast,expected', [
    (4,True,'matched'),(4,False,'matched'),(3,True,'matched'),
    (3,False,'violated'),(2,True,'violated'),(None,True,'unknown'),
    (3,None,'unknown'),(None,False,'unknown'),
])
def test_nested_or_and(stars,breakfast,expected):
    # (4 stars) OR (3 stars AND breakfast)
    a='unknown' if stars is None else ('matched' if stars>=4 else 'violated')
    b='unknown' if stars is None else ('matched' if stars>=3 else 'violated')
    c='unknown' if breakfast is None else ('matched' if breakfast else 'violated')
    groups=[grp('three_and_breakfast','AND',['three','breakfast']),grp('root','OR',['four','three_and_breakfast'])]
    result=evaluate_tree(groups,[ev('four',a),ev('three',b),ev('breakfast',c)])
    assert result.group_status['root']==expected


def test_cycle_must_not_be_confirmed():
    groups=[grp('a','OR',['b','known']),grp('b','AND',['a','known'])]
    result=evaluate_tree(groups,[ev('known','matched')])
    assert result.group_status['a']=='unknown'
    assert result.group_status['b']=='unknown'


def test_required_unknown_not_matched():
    assert rank_class({'req':'unknown','pref':'matched'},['req'])==2


def test_required_violation_never_promoted_by_preference():
    assert rank_class({'req':'violated','pref':'matched'},['req'])==3


def test_invalid_delta_preserves_prior_form():
    base=StructuredForm(budget_amount='40000',budget_currency='RUB',budget_required=True)
    with pytest.raises(ValidationError):
        InterpretationDelta.model_validate({'adults':{'value':'oops','span':'нас двое'}})
    assert str(base.budget_amount)=='40000'


def test_rejected_span_preserves_budget():
    base=StructuredForm(budget_amount='40000',budget_currency='RUB',budget_required=True)
    form,report=apply({'budget':{'amount':'99999','currency':'RUB','required':True,'span':'бюджет 99999'}},'Привет',base)
    assert form==base
    assert 'budget' in report.rejected

@pytest.mark.parametrize('payload,text,ages',[
 ({'children_ages':{'value':[3,14],'span':'дети 3 и 14 лет'}},'дети 3 и 14 лет',[3,14]),
 ({'children_ages':{'value':[0],'span':'младенец 0 лет'}},'младенец 0 лет',[0]),
 ({'children_ages':{'value':[3,14],'span':'дети 3 и 14 лет'}},'другие слова',[]),
])
def test_child_ages_evidence_and_validation(payload,text,ages):
    form,_=apply(payload,text)
    assert form.children_ages==ages


@pytest.mark.parametrize('payload',[
 {'children_ages':{'value':[18],'span':'ребёнок 18 лет'}},
 {'children_ages':{'value':[-1],'span':'младенец'}},
 {'budget':{'amount':'nan','currency':'RUB','span':'nan'}},
 {'transport':{'include':['teleport'],'span':'телепорт'}},
])
def test_invalid_schema_is_rejected(payload):
    with pytest.raises(ValidationError):
        InterpretationDelta.model_validate(payload)
