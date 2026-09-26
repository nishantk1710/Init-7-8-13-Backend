"""Temporary NORMAL criticality as the OAR service-level policy input.

Until Vedanta signs the real Criticality x Service Level matrix, the OAR
estimate's own service-level gate resolves at a fixed NORMAL criticality --
purely to decide whether *some* service level is resolvable, unblocking the
existing service-level -> Z-score -> inventory chain for development. This
must never touch any material's actual criticality, never invent a numeric
Z-score, and never affect non-OAR (Phase 5) behaviour.
"""

from app.initiatives.i7.contracts import Criticality
from app.initiatives.i7.oar.service import (
    OAR_TEMPORARY_CRITICALITY,
    _oar_service_level_configured,
)
from app.initiatives.i7.policy import PolicyDocument, ServiceLevelKey, ServiceLevelPolicy
from app.initiatives.i7.policy.dev_fixtures import load_mock_service_level_policy


def test_temporary_criticality_constant_is_normal():
    assert OAR_TEMPORARY_CRITICALITY is Criticality.NORMAL


def test_unsigned_matrix_blocks_the_oar_gate():
    """The default, unsigned PolicyDocument must still block -- the temporary
    policy input does not conjure a service level out of nothing."""
    policy = PolicyDocument()
    assert policy.service_level.is_configured is False
    assert _oar_service_level_configured(policy) is False


def test_signed_matrix_with_a_normal_entry_unblocks_the_oar_gate():
    policy = PolicyDocument(
        service_level=ServiceLevelPolicy(
            matrix=((ServiceLevelKey(criticality=Criticality.NORMAL), 0.85),)
        )
    )
    assert _oar_service_level_configured(policy) is True


def test_signed_matrix_without_a_normal_entry_still_blocks():
    """Signed, but silent on NORMAL specifically -- the temporary policy input
    must not silently fall back to some other tier's number."""
    policy = PolicyDocument(
        service_level=ServiceLevelPolicy(
            matrix=((ServiceLevelKey(criticality=Criticality.CRITICAL), 0.98),)
        )
    )
    assert _oar_service_level_configured(policy) is False


def test_dev_mock_matrix_unblocks_the_oar_gate():
    """Reuses the existing development/mock service-level fixture mechanism --
    no second hardcoded matrix is introduced."""
    policy = PolicyDocument(service_level=load_mock_service_level_policy())
    assert policy.service_level.is_configured is True
    assert _oar_service_level_configured(policy) is True


def test_no_hardcoded_z_score_in_the_oar_gate():
    """The gate resolves a service-level *fraction*, never a Z-score itself --
    Z is still derived downstream by inventory.service_level.z_factor, the
    same scipy-based call Phase 5 already uses. Pinned by inspecting the
    gate's own source for the absence of any Z/scipy reference."""
    import inspect

    from app.initiatives.i7.oar import service as oar_service_module

    source = inspect.getsource(oar_service_module._oar_service_level_configured)
    assert "z_factor" not in source
    assert "scipy" not in source
    assert "norm.ppf" not in source


def test_gate_does_not_read_or_modify_material_feature_criticality():
    """The temporary policy input is not derived from, and cannot write to,
    any material's own criticality -- the function takes only a policy."""
    import inspect

    from app.initiatives.i7.oar import service as oar_service_module

    parameters = inspect.signature(
        oar_service_module._oar_service_level_configured
    ).parameters
    assert list(parameters) == ["policy"]


def test_temporary_marker_is_documented_as_temporary():
    """A minimal, deliberately fragile guard: the module must say TEMPORARY
    near the constant, so removing that word is a visible, reviewable change
    the day the real VZI rule replaces it."""
    import inspect

    from app.initiatives.i7.oar import service as oar_service_module

    module_source = inspect.getsource(oar_service_module)
    assert "TEMPORARY" in module_source
    assert "OAR_TEMPORARY_CRITICALITY" in module_source


def test_non_oar_inventory_service_module_is_untouched_by_the_temporary_policy():
    """The normal-material Phase 5 path (inventory/service.py) must have no
    reference to the OAR temporary constant or gate -- it keeps resolving
    each material's own real criticality via
    ``inventory.service_level.resolve``, exactly as before this task."""
    import inspect

    from app.initiatives.i7.inventory import service as inventory_service_module

    source = inspect.getsource(inventory_service_module)
    assert "OAR_TEMPORARY_CRITICALITY" not in source
    assert "_oar_service_level_configured" not in source
    # The normal path's own criticality resolution is unchanged: it still
    # reads row.criticality (the material's real value), never a constant.
    assert "_criticality(row.criticality)" in source


def test_material_feature_model_has_no_new_criticality_override():
    """MaterialFeature.criticality itself is untouched -- no default, no
    coercion to NORMAL, was added to the model."""
    from app.models.i7_features import MaterialFeature

    column = MaterialFeature.__table__.columns["criticality"]
    assert column.default is None
    assert column.nullable is True


def test_production_default_policy_does_not_silently_configure_normal():
    """The plain, unsigned PolicyDocument() -- what production uses by
    default -- must NOT have a service-level matrix that treats NORMAL as a
    signed business decision. The temporary gate blocks exactly as before
    until a real (or explicitly dev-mock) sign-off exists."""
    policy = PolicyDocument()
    assert policy.service_level.is_configured is False
    assert _oar_service_level_configured(policy) is False
