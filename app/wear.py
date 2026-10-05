"""Remaining-life calculation.

Claude estimates *what it sees* (tread depth, wear pattern, defects).
The kilometre numbers are computed here, deterministically, so they are
consistent, explainable and unit-testable. Safety rules always win over
the arithmetic.
"""

from __future__ import annotations

import datetime as dt

from .schemas import (
    Confidence,
    RemainingLifeEstimate,
    Severity,
    TyreVisionAssessment,
    Verdict,
    VehicleType,
    WearIndicatorState,
    WearPattern,
)

# Relative tread life vs. a typical passenger car.
VEHICLE_WEAR_FACTOR: dict[VehicleType, float] = {
    VehicleType.car: 1.0,
    VehicleType.suv: 0.85,
    VehicleType.van: 0.75,
    VehicleType.light_truck: 0.75,
    VehicleType.motorcycle: 0.5,
}

PATTERN_ADVICE: dict[WearPattern, str] = {
    WearPattern.center_wear: "Centre wear usually means over-inflation. Check pressures against the door-jamb placard.",
    WearPattern.both_edges: "Wear on both shoulders usually means under-inflation. Check pressures regularly.",
    WearPattern.inner_edge: "Inner-edge wear suggests a camber/alignment problem. Have the wheel alignment checked.",
    WearPattern.outer_edge: "Outer-edge wear suggests alignment or hard cornering. Have the alignment checked.",
    WearPattern.feathering: "Feathered tread usually indicates toe misalignment. Have the alignment checked.",
    WearPattern.cupping: "Cupping points to worn shocks/struts or wheel imbalance. Have the suspension inspected.",
    WearPattern.flat_spot: "A flat spot indicates a skid or locked brakes. Have the tyre and brakes inspected.",
}

IRREGULAR_PATTERNS = set(PATTERN_ADVICE)

_ORDER = [Verdict.ok, Verdict.monitor, Verdict.replace_soon, Verdict.replace_now]


def _at_least(current: Verdict, floor: Verdict) -> Verdict:
    return floor if _ORDER.index(floor) > _ORDER.index(current) else current


def _round_km(km: float) -> int:
    return int(round(max(0.0, km) / 500.0) * 500)


def estimate_remaining_life(
    assessment: TyreVisionAssessment,
    *,
    legal_min_mm: float,
    recommended_min_mm: float,
    default_new_mm: float,
    default_km_per_mm: float,
    vehicle_type: VehicleType = VehicleType.car,
    km_since_fitted: int | None = None,
    tread_when_new_mm: float | None = None,
    today: dt.date | None = None,
) -> RemainingLifeEstimate:
    today = today or dt.date.today()
    warnings: list[str] = []
    advice: list[str] = []

    if not assessment.is_tyre_photo or not assessment.image_usable:
        reasons = assessment.image_issues or ["The photo could not be assessed."]
        return RemainingLifeEstimate(
            verdict=Verdict.unable_to_assess,
            min_tread_mm=0.0,
            legal_min_tread_mm=legal_min_mm,
            km_to_legal_limit=0,
            km_to_legal_limit_low=0,
            km_to_legal_limit_high=0,
            km_to_recommended_limit=0,
            km_per_mm_used=0.0,
            wear_rate_source="none",
            warnings=reasons,
            advice=[
                "Take a sharp, well-lit photo looking straight at the tread, with a coin or "
                "tread-depth gauge in the groove for scale."
            ],
        )

    d = assessment.tread_depth
    min_mm = max(0.0, min(d.inner_mm, d.center_mm, d.outer_mm))
    uncertainty = max(0.3, assessment.depth_uncertainty_mm)
    if assessment.confidence == Confidence.low:
        uncertainty = max(uncertainty, 1.0)
        warnings.append(
            "Low-confidence photo estimate. Confirm with a tread-depth gauge before relying on it."
        )

    # ---- wear rate --------------------------------------------------------
    base_kpm = default_km_per_mm * VEHICLE_WEAR_FACTOR[vehicle_type]
    new_mm = tread_when_new_mm or default_new_mm
    worn_mm = new_mm - min_mm
    if km_since_fitted and km_since_fitted >= 2000 and worn_mm >= 0.5:
        kpm = km_since_fitted / worn_mm
        kpm = min(max(kpm, base_kpm * 0.4), base_kpm * 2.5)  # reject implausible inputs
        source = "measured_from_customer_data"
    else:
        kpm = base_kpm
        source = "default_for_vehicle_type"

    if assessment.wear_pattern in IRREGULAR_PATTERNS:
        kpm *= 0.85  # irregular wear accelerates until the cause is fixed
        advice.append(PATTERN_ADVICE[assessment.wear_pattern])

    usable_mm = min_mm - legal_min_mm
    km = usable_mm * kpm
    km_low = (min_mm - uncertainty - legal_min_mm) * kpm * 0.85
    km_high = (min_mm + uncertainty - legal_min_mm) * kpm * 1.15
    km_rec = (min_mm - recommended_min_mm) * kpm

    # ---- verdict ----------------------------------------------------------
    verdict = Verdict.ok
    if min_mm <= recommended_min_mm + 1.5:
        verdict = Verdict.monitor
    if min_mm <= recommended_min_mm:
        verdict = Verdict.replace_soon
        warnings.append(
            f"Tread is at or below {recommended_min_mm:g} mm. Wet-weather grip is noticeably reduced."
        )

    critical = [x for x in assessment.defects if x.severity == Severity.critical]
    major = [x for x in assessment.defects if x.severity == Severity.major]
    if major:
        verdict = _at_least(verdict, Verdict.replace_soon)
        for x in major:
            warnings.append(f"{x.type.value.replace('_', ' ').capitalize()} ({x.location.value}): {x.description}")
        advice.append("Have a tyre professional inspect the damage before any long trip.")

    at_limit = min_mm <= legal_min_mm or assessment.wear_indicator == WearIndicatorState.flush
    if critical or at_limit:
        verdict = Verdict.replace_now
        km = km_low = km_high = km_rec = 0
        for x in critical:
            warnings.append(
                f"UNSAFE: {x.type.value.replace('_', ' ')} on the {x.location.value}. {x.description}"
            )
        if at_limit:
            warnings.append(f"Tread is at or below the legal limit of {legal_min_mm:g} mm.")
        advice.insert(0, "Do not drive on this tyre. Replace it now or fit the spare.")

    # ---- age --------------------------------------------------------------
    if assessment.manufacture_year > 1990:
        age = today.year - assessment.manufacture_year
        if assessment.manufacture_week and today.isocalendar().week < assessment.manufacture_week:
            age -= 1
        if age >= 10:
            verdict = _at_least(verdict, Verdict.replace_soon)
            warnings.append(f"Tyre is about {age} years old. Rubber ages even with good tread; replace it.")
        elif age >= 6:
            warnings.append(f"Tyre is about {age} years old. Have it inspected yearly for cracking.")

    return RemainingLifeEstimate(
        verdict=verdict,
        min_tread_mm=round(min_mm, 1),
        legal_min_tread_mm=legal_min_mm,
        km_to_legal_limit=_round_km(km),
        km_to_legal_limit_low=_round_km(km_low),
        km_to_legal_limit_high=_round_km(km_high),
        km_to_recommended_limit=_round_km(km_rec),
        km_per_mm_used=round(kpm),
        wear_rate_source=source,
        warnings=warnings,
        advice=advice,
    )


VERDICT_TEXT = {
    Verdict.ok: "Your tyre looks to be in good condition.",
    Verdict.monitor: "Your tyre is usable but getting worn. Keep an eye on it.",
    Verdict.replace_soon: "Plan to replace this tyre soon.",
    Verdict.replace_now: "This tyre should be replaced now.",
    Verdict.unable_to_assess: "I couldn't assess this photo reliably.",
}


def summarise(estimate: RemainingLifeEstimate) -> str:
    parts = [VERDICT_TEXT[estimate.verdict]]
    if estimate.verdict not in (Verdict.unable_to_assess, Verdict.replace_now):
        parts.append(
            f"Estimated shallowest tread: {estimate.min_tread_mm:g} mm. "
            f"Roughly {estimate.km_to_legal_limit:,} km left before the legal limit "
            f"({estimate.km_to_legal_limit_low:,}–{estimate.km_to_legal_limit_high:,} km), "
            f"and about {estimate.km_to_recommended_limit:,} km before the recommended replacement point."
        )
    return " ".join(parts)
