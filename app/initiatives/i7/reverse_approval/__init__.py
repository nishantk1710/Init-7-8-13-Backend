"""Approval of a Min-Max -> OAR conversion request.

A planner asks for one material-plant to be converted to OAR; the request then walks the same fixed chain an OAR
conversion does -- Inventory Controller -> Engineering Head -> Plant Head -> Commercial Head -- under the same
state machine (``app.initiatives.i7.recommendations.workflow``), with its own tables. Approval is a recorded
decision only: nothing here writes to SAP.

Kept outside ``reverse_conversion/`` on purpose: that package decides and must not depend on the OAR -> Min-Max
code (its independence test enforces it); this one reuses the shared approval machinery and nothing of the
forward conversion's decision logic.
"""
