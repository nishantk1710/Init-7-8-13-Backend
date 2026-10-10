"""The I13 snapshot stored in Azure SQL (``I13_SNAPSHOT_STORE=sql``).

``codec``        dataclass <-> compact JSON for the payload column
``kinds``        what each stored item is and what its filter columns hold
``work_tables``  indexed per-build copies of the SAP views I13 reads
``batch_repos``  repositories over one batch of materials, from those copies
``builder``      the batched build, the version switch, cleanup
``reader``       ``SqlSnapshot``: the routes' filters, counts and pages in SQL
``lifecycle``    when to build, what to serve meanwhile, status
``refresh``      recompute one material-plant in place

See ``app/models/i13_snapshot_store.py`` for why this exists.
"""
