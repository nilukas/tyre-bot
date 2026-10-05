"""openapi.yaml is the contract: make sure the Pydantic models match it."""

from pathlib import Path

import pytest
import yaml

from app import schemas

SPEC = yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi.yaml").read_text())
COMPONENTS = SPEC["components"]["schemas"]

MODELS = [
    schemas.Me, schemas.TreadDepths, schemas.Defect, schemas.TyreVisionAssessment,
    schemas.RemainingLifeEstimate, schemas.AnalysisResult, schemas.ChatRequest,
    schemas.ChatReply, schemas.ReferenceTyre, schemas.ReferenceList,
]
ENUMS = [schemas.VehicleType, schemas.ReferenceCondition, schemas.Verdict]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_model_fields_match_spec(model):
    spec = COMPONENTS[model.__name__]
    assert set(spec["properties"]) == set(model.model_fields)
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert set(spec.get("required", [])) == required


@pytest.mark.parametrize("enum", ENUMS, ids=lambda e: e.__name__)
def test_enums_match_spec(enum):
    assert set(COMPONENTS[enum.__name__]["enum"]) == {e.value for e in enum}
