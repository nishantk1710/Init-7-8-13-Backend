"""Shared OAR / Min-Max / Excluded material-scope policy.

Genuinely cross-initiative: consumed today by I13 and intended for later reuse
by I07 and any reservation-time assistant/router. See policy.py.
"""

from app.shared.material_scope.policy import MaterialScope, build_scope_index, classify_material_scope

__all__ = ["MaterialScope", "build_scope_index", "classify_material_scope"]
