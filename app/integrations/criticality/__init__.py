"""Criticality providers. One module per backing system.

The port, the errors and the factory live in ``app.core.criticality``; these are
the concrete sources it builds. Nothing outside this package should import a
source directly -- initiatives call ``get_criticality_source()``.
"""
