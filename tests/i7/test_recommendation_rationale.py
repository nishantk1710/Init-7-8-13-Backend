"""AI-generated recommendation rationale, with a deterministic fallback.

Rationale generation must never block recommendation calculation, must
never claim AI generation when the stub or a fallback answered, and must
never let the model decide a business value -- it only explains numbers
Phase 3/4/5/6 already computed.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.ai import AIError, AINotConfiguredError, AITimeoutError, Completion
from app.initiatives.i7.contracts.recommendation import RecommendationFactor
from app.initiatives.i7.recommendations import rationale
from app.initiatives.i7.recommendations.builder import BuiltRecommendation
from app.initiatives.i7.recommendations.types import (
    ConversionDecision,
    ConversionEligibility,
    ConversionTrigger,
    ExpectedImpact,
    ImpactStatus,
    LifecycleStatus,
)


def _built(**overrides) -> BuiltRecommendation:
    defaults = dict(
        sap_material_number="1000000000",
        sap_plant_code="1300",
        status=LifecycleStatus.READY_FOR_REVIEW,
        is_oar=False,
        demand_class="SMOOTH",
        history_status="SUFFICIENT",
        criticality="CRITICAL",
        current_safety_stock=Decimal(5),
        current_rop=Decimal(10),
        current_max_stock=None,
        recommended_safety_stock=Decimal(8),
        recommended_rop=Decimal(15),
        recommended_max_stock=None,
        baseline_model="SES",
        forecast_rate=Decimal("12.5"),
        lead_time_method="PLANNED_FALLBACK",
        safety_stock_method="normal_path",
        max_stock_strategy=None,
        confidence=None,
        oar_neighbour_count=None,
        oar_best_similarity=None,
        circuit=None,
        unit_price=None,
        lead_time_days=Decimal(30),
        lead_time_variance_days=Decimal(5),
        service_level=Decimal("0.95"),
        z_factor=Decimal("1.645"),
        conversion=None,
        blocking_reason=None,
        factors=(RecommendationFactor(label="Demand pattern", detail="Demand class is SMOOTH."),),
        calculation_trace=(),
        impact=ExpectedImpact(status=ImpactStatus.AVAILABLE),
        feature_run_id=1,
        forecast_run_id=1,
        inventory_run_id=1,
        oar_run_id=None,
        policy_id="i07-default",
        policy_version=1,
    )
    defaults.update(overrides)
    return BuiltRecommendation(**defaults)


def _oar_built(**overrides) -> BuiltRecommendation:
    defaults = dict(
        is_oar=True,
        demand_class="UNCLASSIFIED",
        history_status="COLD_START",
        criticality=None,
        recommended_safety_stock=Decimal(6),
        recommended_rop=Decimal(9),
        oar_neighbour_count=5,
        oar_best_similarity=Decimal("0.72"),
        conversion=ConversionDecision(
            eligibility=ConversionEligibility.ELIGIBLE,
            trigger=ConversionTrigger.CONSUMPTION_FREQUENCY,
            consumption_count_12m=7,
            consumption_count_threshold=4,
            production_impact=None,
            i13_hod_approved=None,
            detail="consumption count 7 > 4",
        ),
        factors=(
            RecommendationFactor(
                label="OAR cold-start",
                detail="Material qualifies for the OAR cold-start path.",
            ),
        ),
    )
    defaults.update(overrides)
    return _built(**defaults)


def _fake_completion(text: str, provider: str = "foundry", model: str = "gpt-4o-mini") -> Completion:
    return Completion(text=text, model=model, provider=provider)


# --- I07 calls the existing gateway, no provider SDK imported ---------------


def test_i07_recommendations_package_does_not_import_a_provider_sdk():
    import ast
    import inspect
    from pathlib import Path

    package_dir = Path(rationale.__file__).parent
    forbidden = {"openai", "azure", "anthropic"}
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not (imported & forbidden), path.name


def test_rationale_module_only_imports_the_core_ai_gateway():
    """Confirms the actual call path: app.core.ai / app.core.prompts, never
    app.integrations.ai.* directly. Checked against real import statements
    (AST), not docstring prose, which legitimately names
    app.integrations.ai when explaining the architecture."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rationale))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert any(m == "app.core.ai" for m in imported_modules)
    assert any(m == "app.core.prompts" for m in imported_modules)
    assert not any(m.startswith("app.integrations.ai") for m in imported_modules)


# --- Deterministic fallback is preserved, never deleted ---------------------


def test_deterministic_text_uses_the_existing_factors():
    built = _built(status=LifecycleStatus.NOT_EVALUABLE, recommended_rop=None, recommended_safety_stock=None)
    result = rationale.generate_rationale(built)
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK
    assert "SMOOTH" in result.text


def test_nothing_computed_skips_the_ai_call_entirely():
    built = _built(recommended_rop=None, recommended_safety_stock=None)
    with patch("app.initiatives.i7.recommendations.rationale.complete_with_prompt") as mock_call:
        result = rationale.generate_rationale(built)
    mock_call.assert_not_called()
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK


def test_not_evaluable_recommendation_causes_zero_ai_calls():
    """A NOT_EVALUABLE recommendation -- the actual lifecycle status the
    builder assigns whenever safety stock/ROP (normal path) or the OAR
    estimate did not succeed -- must never reach the LLM, not just a
    recommendation with the None/None proxy fields checked in isolation."""
    built = _built(
        status=LifecycleStatus.NOT_EVALUABLE,
        recommended_rop=None,
        recommended_safety_stock=None,
        blocking_reason="safety stock status=NOT_EVALUABLE_SERVICE_LEVEL_UNSET, rop status=NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
    )
    with patch("app.initiatives.i7.recommendations.rationale.complete_with_prompt") as mock_call:
        result = rationale.generate_rationale(built)
    assert mock_call.call_count == 0
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK


def test_reviewable_recommendation_causes_exactly_one_ai_call():
    """A READY_FOR_REVIEW recommendation with both values computed must call
    the gateway exactly once -- not zero (it must try), not more than once
    (no retry-as-a-second-call, no double invocation per row)."""
    built = _built(status=LifecycleStatus.READY_FOR_REVIEW)
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        return_value=_fake_completion("A real explanation.", provider="foundry"),
    ) as mock_call:
        result = rationale.generate_rationale(built)
    assert mock_call.call_count == 1
    assert result.source == rationale.RATIONALE_SOURCE_AI


# --- Provider failure modes all fall back, never raise ---------------------


@pytest.mark.parametrize(
    "error",
    [
        AINotConfiguredError("no provider configured"),
        AITimeoutError("timed out"),
        AIError("generic AI failure"),
    ],
)
def test_provider_failure_triggers_deterministic_fallback(error):
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt", side_effect=error
    ):
        result = rationale.generate_rationale(built)
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK
    assert result.text  # never empty


def test_unexpected_exception_also_falls_back_without_raising():
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        side_effect=RuntimeError("boom"),
    ):
        result = rationale.generate_rationale(built)  # must not raise
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK


def test_invalid_empty_response_triggers_fallback():
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        return_value=_fake_completion("   ", provider="foundry"),
    ):
        result = rationale.generate_rationale(built)
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK


def test_recommendation_calculation_is_unaffected_by_ai_failure():
    """The BuiltRecommendation's own computed values are untouched regardless
    of what rationale generation does -- proving failure isolation."""
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        side_effect=AITimeoutError("timeout"),
    ):
        rationale.generate_rationale(built)
    assert built.recommended_rop == Decimal(15)
    assert built.recommended_safety_stock == Decimal(8)


# --- Stub provider is never mistaken for a real model -----------------------


def test_stub_provider_output_is_deterministic_fallback_not_ai_generated():
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        return_value=_fake_completion("stub text here", provider="stub"),
    ):
        result = rationale.generate_rationale(built)
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK


# --- A real (mocked) provider success is labelled AI_GENERATED -------------


def test_foundry_provider_success_is_ai_generated():
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        return_value=_fake_completion("A real explanation.", provider="foundry", model="gpt-4o-mini"),
    ):
        result = rationale.generate_rationale(built)
    assert result.source == rationale.RATIONALE_SOURCE_AI
    assert result.text == "A real explanation."
    assert result.provider == "foundry"
    assert result.model == "gpt-4o-mini"


def test_openai_compatible_provider_success_is_ai_generated():
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        return_value=_fake_completion("Alternate provider text.", provider="openai", model="gpt-4o"),
    ):
        result = rationale.generate_rationale(built)
    assert result.source == rationale.RATIONALE_SOURCE_AI
    assert result.provider == "openai"


# --- OAR rationale includes OAR evidence, never eligibility itself --------


def test_oar_rationale_uses_the_oar_prompt_and_context():
    built = _oar_built()
    captured = {}

    def fake_complete(prompt_id, *, task=None, **context):
        captured["prompt_id"] = prompt_id
        captured.update(context)
        return _fake_completion("OAR explanation.", provider="foundry")

    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        side_effect=fake_complete,
    ):
        result = rationale.generate_rationale(built)

    assert captured["prompt_id"] == "i07_oar_conversion_rationale"
    assert captured["neighbour_count"] == "5"
    assert captured["best_similarity"] == "0.72"
    assert captured["consumption_count_12m"] == "7"
    assert captured["demand_class"] == "UNCLASSIFIED"
    assert result.source == rationale.RATIONALE_SOURCE_AI


def test_oar_rationale_context_never_includes_an_eligibility_field():
    """The LLM explains the trigger evidence; it never receives a field that
    would let it assert or overturn eligibility itself."""
    built = _oar_built()
    context = rationale._oar_context(built)
    assert "eligibility" not in context
    assert "conversion_eligibility" not in context


def test_llm_cannot_modify_recommended_values():
    """No matter what the (mocked) provider returns, BuiltRecommendation's
    own recommended_rop/recommended_safety_stock are untouched -- the LLM
    output only ever becomes rationale text, never a business value."""
    built = _oar_built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        return_value=_fake_completion(
            "Recommended ROP should actually be 999.", provider="foundry"
        ),
    ):
        rationale.generate_rationale(built)
    assert built.recommended_rop == Decimal(9)
    assert built.recommended_safety_stock == Decimal(6)


# --- No secrets in the result -----------------------------------------------


def test_rationale_result_carries_no_credentials():
    built = _built()
    with patch(
        "app.initiatives.i7.recommendations.rationale.complete_with_prompt",
        return_value=_fake_completion("Safe text.", provider="foundry"),
    ):
        result = rationale.generate_rationale(built)
    result_dict = result.__dict__
    for forbidden in ("api_key", "secret", "token", "credential", "authorization"):
        assert forbidden not in str(result_dict).lower()
