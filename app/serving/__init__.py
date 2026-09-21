"""Building the serving layer from the raw layer.

    odata_*  --(app.normalise)-->  material_plant, ...

Built in Python rather than as ``INSERT ... SELECT``. That is the slower
option and it is the deliberate one: the interesting logic is the material
number padding rule and the decimal coercion, both of which are conditional in
ways that are painful to express in T-SQL and awkward to test there. Keeping
them as ordinary functions means they are covered by tests that need no
database, which matters more than the seconds a set-based build would save
across a few thousand rows.

If a fact table ever grows past what this can stream comfortably, the answer is
to move that table's build to SQL -- not to give up the tested normalise
functions for all of them.

Rebuilt whole, not merged. The serving layer is derived data: everything in it
can be recomputed from ``odata_*``, so there is nothing to preserve across a
rebuild and nothing to reconcile. That is what makes it safe to run at any
time.
"""
