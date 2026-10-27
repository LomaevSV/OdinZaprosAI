"""Cross-currency component evaluation must not invent budget currency."""
from decimal import Decimal
from odin.contracts import Constraint
from odin.core.constraints import OfferFacts, evaluate_constraint


def test_eur_budget_vs_rub_offer_must_not_be_treated_as_rub_budget():
    constraint=Constraint(constraint_id='c-budget_total',field='budget_total',operator='lte',value='100',requiredness='required',source_span='до 100 евро',source_kind='text_span',scope='trip',confidence=1,ambiguity=None)
    facts=OfferFacts(component_id='hotel-1',role='hotel',total_amount=Decimal('500'),currency='RUB',price_completeness='complete')
    result=evaluate_constraint(constraint,facts)
    assert result.status == 'unknown', result
