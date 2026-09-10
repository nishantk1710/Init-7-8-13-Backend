"""Business modules of the single Spares AI product.

Boundary rule: I07, I08 and I13 must not import each other's internals.
Anything common belongs in ``app.shared``; anything external belongs in
``app.integrations``.
"""
