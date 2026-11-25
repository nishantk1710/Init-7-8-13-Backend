"""Min-Max -> OAR recommendations (reverse conversion).

A feature of its own. It is independent of the OAR -> Min-Max conversion
(``app.initiatives.i7.recommendations.conversion``) and must stay so: this package never imports that
module, its policy, or the adoption modules, and a test enforces it.

Phase 1 holds the pure business logic -- ``metrics`` (consumption and the calculation anchor) and
``evaluate`` (the decision table). Data access, the service and the API come after.
"""
