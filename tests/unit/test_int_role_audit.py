from datetime import UTC, datetime
import pytest
from odin.contracts.api import StructuredForm
from odin.llm.free_text import InterpretationDelta, apply_delta

@pytest.mark.parametrize('text,origin,destination', [
 ('Из Москвы в Казань','Казань','Москва'),
 ('В Казань из Москвы','Казань','Москва'),
 ('Хочу из Перми поехать в Орёл','Орёл','Пермь'),
])
def test_city_roles_cannot_be_swapped(text,origin,destination):
    delta=InterpretationDelta.model_validate({'origin':{'value':origin,'span':text},'destination':{'value':destination,'span':text}})
    form,report=apply_delta(delta,text=text,base=StructuredForm(),reference_datetime=datetime(2026,10,25,tzinfo=UTC))
    assert form.origin != origin or form.destination != destination, report
