"""Initiative 07 -- Predictive Inventory & Safety Stock Optimization.

Layout::

    contracts/   canonical, source-independent domain types
    policy/      versioned business rules and their validation
    errors.py    domain error hierarchy

Two boundaries hold this module together, and both are load-bearing.

**The source boundary.** Contracts know nothing about SAP. Adapters (Phase 2 for
the extract, Phase 12 for live OData) map a source onto the contracts; the
domain reads only contracts. Swapping the source is then an adapter change, not
a rewrite of forecasting and calculation.

**The policy boundary.** No business threshold is written into calculation code.
Rules live in a versioned :class:`~app.initiatives.i7.policy.PolicyDocument`,
and every recommendation names the version that produced it. Several of those
rules are still unresolved at Vedanta; they are represented as explicitly unset
and block recommendations, rather than carrying a default that would look like a
decision nobody made.

I07 imports nothing from I08 or I13, and neither imports from here.
"""
