"""W6.6 ``EscalationRecipientProvider`` adapters.

TODAY: no Entra ID/DOA (delegation-of-authority) source is integrated
anywhere in this codebase (``app/integrations/entra/__init__.py`` is an
unimplemented stub, and ``app/core/security.py`` documents that
authentication itself isn't wired in yet). ``ConfigEscalationRecipientProvider``
is a local/config mapping -- plant -> HOD identity -- read from
``Settings.i13_hod_recipients`` (comma-separated ``PLANT:identity`` pairs,
the same parsing convention as ``Settings.i13_oar_mrp_types``). It is a
placeholder for a real DOA mapping, not a modelling of one: this codebase
has no delegation-of-authority table or hierarchy today.

LATER: an Entra/DOA-backed adapter implements the same
``EscalationRecipientProvider.get_hod`` method -- ``app.initiatives.i13.act
.service.process_escalations`` does not change, and does not import
``app.integrations.entra`` or any Azure SDK.

Neither adapter ever fabricates a recipient: unresolved always returns
``None``, which ``process_escalations`` persists as an explicit
``ROUTING_PENDING`` state rather than guessing.
"""

from __future__ import annotations


class NullEscalationRecipientProvider:
    """No HOD/DOA source is configured -- every lookup is unresolved."""

    def get_hod(self, *, material: str, plant: str, requester_id: str | None) -> str | None:
        return None


class ConfigEscalationRecipientProvider:
    """Local/config plant -> HOD mapping, parsed once at construction from
    ``Settings.i13_hod_recipients`` (see ``app.core.config.Settings``).
    Falls back to ``None`` (routing pending) for any plant not present in
    the mapping -- never invents a recipient for an unmapped plant."""

    def __init__(self, plant_to_hod: dict[str, str]) -> None:
        self._plant_to_hod = dict(plant_to_hod)

    @classmethod
    def from_config_string(cls, raw: str) -> "ConfigEscalationRecipientProvider":
        """``raw`` is ``"PLANT:identity,PLANT2:identity2"`` -- the same
        comma-separated convention ``Settings.i13_oar_mrp_types`` uses.
        Malformed entries (no ``:``) are skipped, never guessed at."""
        mapping: dict[str, str] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            plant, _, hod = pair.partition(":")
            plant = plant.strip().upper()
            hod = hod.strip()
            if plant and hod:
                mapping[plant] = hod
        return cls(mapping)

    def get_hod(self, *, material: str, plant: str, requester_id: str | None) -> str | None:
        return self._plant_to_hod.get(plant.upper())
