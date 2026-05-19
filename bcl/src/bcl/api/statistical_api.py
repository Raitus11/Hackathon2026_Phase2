"""Statistical equivalence-testing endpoints.

POST /statistical/welch         Welch's two-sample t-test on latency
POST /statistical/chi-square    Pearson chi-square GOF on dispositions
POST /statistical/validate      both tests + a combined PASS/WARN/FAIL

These turn "validation passed" into a signed statistical statement:
the post-migration system behaves like the pre-migration system to
within measurement noise. The latency samples are the round-trip
durations the message-flow harness (`POST /topologies/{id}/flows/
{n}/send`) already returns as `total_duration_seconds` — the caller
collects a handful of sends before the migration and a handful after,
and posts the two arrays here.

References: Welch (1947), Biometrika 34(1/2); Pearson (1900),
Philosophical Magazine 50(302). See bcl.analysis.statistical for the
method and the explicit honest-scope rationale (why these two tests
and not KL divergence or CUSUM).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from bcl.analysis.statistical import chi_square_gof, welch_t_test

router = APIRouter(prefix="/statistical", tags=["statistical-validation"])


# ─────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────


class WelchRequest(BaseModel):
    """Pre/post round-trip latency samples (seconds)."""

    pre: list[float] = Field(min_length=1)
    post: list[float] = Field(min_length=1)
    label: str = Field(default="latency", max_length=120)


class ChiSquareRequest(BaseModel):
    """Pre/post message-disposition counts."""

    categories: list[str] = Field(min_length=2)
    observed_post: list[float] = Field(min_length=2)
    expected_pre: list[float] = Field(min_length=2)


class ValidateRequest(BaseModel):
    """Both tests in one call, producing a combined gate decision."""

    latency_pre: list[float] = Field(min_length=1)
    latency_post: list[float] = Field(min_length=1)
    disposition_categories: list[str] = Field(min_length=2)
    disposition_observed_post: list[float] = Field(min_length=2)
    disposition_expected_pre: list[float] = Field(min_length=2)


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.post(
    "/welch",
    summary="Welch's two-sample t-test on pre/post latency samples",
    description=(
        "H0: pre- and post-migration mean latency are equal. Failing "
        "to reject H0 (outcome=PASS) is evidence the migration "
        "preserved latency. Unequal-variance t-test with the Welch-"
        "Satterthwaite df approximation. Reference: Welch (1947)."
    ),
)
async def welch(body: WelchRequest) -> dict[str, Any]:
    result = welch_t_test(body.pre, body.post)
    return {"label": body.label, **result.to_dict()}


@router.post(
    "/chi-square",
    summary="Pearson chi-square goodness-of-fit on message dispositions",
    description=(
        "H0: the post-migration message-disposition distribution "
        "(delivered / DLQ / in-flight) matches the pre-migration "
        "distribution. Failing to reject H0 (outcome=PASS) is evidence "
        "no messages were lost or mis-routed. Reference: Pearson (1900)."
    ),
)
async def chi_square(body: ChiSquareRequest) -> dict[str, Any]:
    result = chi_square_gof(
        body.categories, body.observed_post, body.expected_pre
    )
    return result.to_dict()


@router.post(
    "/validate",
    summary="Combined statistical validation (Welch + chi-square)",
    description=(
        "Runs both tests and folds them into one PASS/WARN/FAIL gate "
        "decision. FAIL if either test FAILs; WARN if either WARNs and "
        "neither FAILs; PASS only if both PASS. The combined verdict is "
        "what a migration validation gate would consume."
    ),
)
async def validate(body: ValidateRequest) -> dict[str, Any]:
    welch_res = welch_t_test(body.latency_pre, body.latency_post)
    chi_res = chi_square_gof(
        body.disposition_categories,
        body.disposition_observed_post,
        body.disposition_expected_pre,
    )

    outcomes = {welch_res.outcome, chi_res.outcome}
    if "FAIL" in outcomes:
        combined = "FAIL"
    elif "WARN" in outcomes:
        combined = "WARN"
    else:
        combined = "PASS"

    return {
        "combined_outcome": combined,
        "summary": (
            f"Statistical validation: {combined}. "
            f"Latency (Welch t-test): {welch_res.outcome}. "
            f"Message disposition (chi-square): {chi_res.outcome}."
        ),
        "welch_t_test": welch_res.to_dict(),
        "chi_square_gof": chi_res.to_dict(),
        "references": [
            "Welch, B. L. (1947). Biometrika 34(1/2), 28-35.",
            "Pearson, K. (1900). Philosophical Magazine 50(302), "
            "157-175.",
        ],
    }


__all__ = ["router"]
