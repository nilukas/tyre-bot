"""Pydantic models.

Two groups:
  * ``TyreVisionAssessment`` and friends: the structured output Claude must
    return for a tyre photo (constrained via ``messages.parse``).
  * API request/response models: these mirror ``openapi.yaml``, which is the
    source-of-truth contract. Keep the two in sync (tests/test_contract.py).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Claude structured output
# --------------------------------------------------------------------------


class WearPattern(str, Enum):
    even = "even"
    center_wear = "center_wear"  # over-inflation
    both_edges = "both_edges"  # under-inflation
    inner_edge = "inner_edge"  # camber / alignment
    outer_edge = "outer_edge"  # camber / cornering
    cupping = "cupping"  # suspension / balance
    feathering = "feathering"  # toe misalignment
    flat_spot = "flat_spot"  # skid / locked brakes
    unknown = "unknown"


class WearIndicatorState(str, Enum):
    not_visible = "not_visible"
    well_above = "well_above"
    near = "near"
    flush = "flush"  # tread is at the legal limit


class DefectType(str, Enum):
    sidewall_bulge = "sidewall_bulge"
    cords_exposed = "cords_exposed"
    cut_or_gash = "cut_or_gash"
    puncture_object = "puncture_object"
    cracking_dry_rot = "cracking_dry_rot"
    tread_separation = "tread_separation"
    chunking = "chunking"
    bead_damage = "bead_damage"
    other = "other"


class DefectLocation(str, Enum):
    tread = "tread"
    shoulder = "shoulder"
    sidewall = "sidewall"
    bead = "bead"
    unknown = "unknown"


class Severity(str, Enum):
    minor = "minor"
    major = "major"
    critical = "critical"  # unsafe to drive


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class TreadDepths(BaseModel):
    inner_mm: float
    center_mm: float
    outer_mm: float


class Defect(BaseModel):
    type: DefectType
    location: DefectLocation
    severity: Severity
    description: str


class TyreVisionAssessment(BaseModel):
    is_tyre_photo: bool
    image_usable: bool
    image_issues: list[str]
    tread_depth: TreadDepths
    depth_uncertainty_mm: float
    wear_pattern: WearPattern
    wear_indicator: WearIndicatorState
    defects: list[Defect]
    dot_code: str  # empty string when not visible
    manufacture_year: int  # 0 when not visible
    manufacture_week: int  # 0 when not visible
    most_similar_reference_ids: list[str]
    confidence: Confidence
    observations: str


# --------------------------------------------------------------------------
# Remaining-life estimate (computed in Python, not by the model)
# --------------------------------------------------------------------------


class Verdict(str, Enum):
    ok = "ok"
    monitor = "monitor"
    replace_soon = "replace_soon"
    replace_now = "replace_now"
    unable_to_assess = "unable_to_assess"


class VehicleType(str, Enum):
    car = "car"
    suv = "suv"
    van = "van"
    light_truck = "light_truck"
    motorcycle = "motorcycle"


class RemainingLifeEstimate(BaseModel):
    verdict: Verdict
    min_tread_mm: float
    legal_min_tread_mm: float
    km_to_legal_limit: int
    km_to_legal_limit_low: int
    km_to_legal_limit_high: int
    km_to_recommended_limit: int
    km_per_mm_used: float
    wear_rate_source: str  # "measured_from_customer_data" | "default_for_vehicle_type"
    warnings: list[str]
    advice: list[str]


# --------------------------------------------------------------------------
# API models (mirror openapi.yaml)
# --------------------------------------------------------------------------


class Me(BaseModel):
    name: str
    is_admin: bool
    csrf_token: str


class AnalysisResult(BaseModel):
    analysis_id: str
    assessment: TyreVisionAssessment
    estimate: RemainingLifeEstimate
    summary: str
    reply: str | None = None
    disclaimer: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatReply(BaseModel):
    reply: str


class ReferenceCondition(str, Enum):
    new = "new"
    good = "good"
    worn = "worn"
    at_limit = "at_limit"
    damaged = "damaged"


class ReferenceTyre(BaseModel):
    id: str
    condition: ReferenceCondition
    tread_depth_mm: float
    km_driven: int | None = None
    tyre_size: str | None = None
    brand_model: str | None = None
    notes: str | None = None
    created_by: str
    created_at: str


class ReferenceList(BaseModel):
    items: list[ReferenceTyre]


class ErrorBody(BaseModel):
    detail: str
