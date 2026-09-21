"""Manual/scheduled report-generation entry points.

Separate from ``app.initiatives.i7.reporting``, which holds the actual
business logic (aggregation service, repository, baseline comparison). This
package holds only CLI wiring that calls into that package -- the same
pattern as ``app.seed`` (a CLI package) versus the seed loader's own logic.
"""
