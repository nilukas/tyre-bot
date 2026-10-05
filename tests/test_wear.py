import datetime as dt

from app.schemas import (
    Confidence,
    Defect,
    DefectLocation,
    DefectType,
    Severity,
    TreadDepths,
    TyreVisionAssessment,
    Verdict,
    VehicleType,
    WearIndicatorState,
    WearPattern,
)
from app.wear import estimate_remaining_life

RULES = dict(legal_min_mm=1.6, recommended_min_mm=3.0, default_new_mm=8.0, default_km_per_mm=8000)


def make(depth=5.0, **overrides) -> TyreVisionAssessment:
    base = dict(
        is_tyre_photo=True, image_usable=True, image_issues=[],
        tread_depth=TreadDepths(inner_mm=depth, center_mm=depth, outer_mm=depth),
        depth_uncertainty_mm=0.5, wear_pattern=WearPattern.even,
        wear_indicator=WearIndicatorState.well_above, defects=[], dot_code="",
        manufacture_year=0, manufacture_week=0, most_similar_reference_ids=[],
        confidence=Confidence.high, observations="",
    )
    base.update(overrides)
    return TyreVisionAssessment(**base)


def test_default_rate_for_car():
    est = estimate_remaining_life(make(5.0), **RULES)
    # (5.0 - 1.6) * 8000 = 27,200 -> rounded to 27,000
    assert est.km_to_legal_limit == 27000
    assert est.km_to_legal_limit_low < est.km_to_legal_limit < est.km_to_legal_limit_high
    assert est.verdict == Verdict.ok
    assert est.wear_rate_source == "default_for_vehicle_type"


def test_uses_shallowest_region():
    a = make(tread_depth=TreadDepths(inner_mm=2.5, center_mm=6.0, outer_mm=6.0),
             wear_pattern=WearPattern.inner_edge)
    est = estimate_remaining_life(a, **RULES)
    assert est.min_tread_mm == 2.5
    assert est.verdict == Verdict.replace_soon
    assert any("alignment" in x for x in est.advice)


def test_measured_wear_rate_from_customer_data():
    # 8 mm -> 5 mm over 30,000 km = 10,000 km/mm
    est = estimate_remaining_life(make(5.0), km_since_fitted=30000, **RULES)
    assert est.wear_rate_source == "measured_from_customer_data"
    assert est.km_per_mm_used == 10000
    assert est.km_to_legal_limit == 34000


def test_motorcycle_wears_faster():
    car = estimate_remaining_life(make(5.0), **RULES)
    moto = estimate_remaining_life(make(5.0), vehicle_type=VehicleType.motorcycle, **RULES)
    assert moto.km_to_legal_limit < car.km_to_legal_limit


def test_critical_defect_overrides_tread():
    a = make(7.0, defects=[Defect(type=DefectType.sidewall_bulge, location=DefectLocation.sidewall,
                                  severity=Severity.critical, description="Bulge near rim")])
    est = estimate_remaining_life(a, **RULES)
    assert est.verdict == Verdict.replace_now
    assert est.km_to_legal_limit == 0
    assert est.km_to_legal_limit_high == 0


def test_at_legal_limit():
    est = estimate_remaining_life(make(1.5), **RULES)
    assert est.verdict == Verdict.replace_now
    assert est.km_to_legal_limit == 0


def test_wear_indicator_flush_means_replace():
    est = estimate_remaining_life(make(2.2, wear_indicator=WearIndicatorState.flush), **RULES)
    assert est.verdict == Verdict.replace_now


def test_old_tyre_flagged():
    est = estimate_remaining_life(make(7.0, manufacture_year=2014, manufacture_week=10),
                                  today=dt.date(2026, 10, 4), **RULES)
    assert est.verdict == Verdict.replace_soon
    assert any("years old" in w for w in est.warnings)


def test_unusable_photo():
    est = estimate_remaining_life(make(image_usable=False, image_issues=["Too blurry"]), **RULES)
    assert est.verdict == Verdict.unable_to_assess
    assert est.warnings == ["Too blurry"]


def test_implausible_customer_km_is_clamped():
    # 100 km for 3 mm of wear would be absurd; the rate is clamped to the plausible band
    est = estimate_remaining_life(make(5.0), km_since_fitted=2000, **RULES)
    assert est.km_per_mm_used >= 8000 * 0.4
