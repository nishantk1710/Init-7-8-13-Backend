"""Ingestion from live SAP, through CPI, into the raw layer.

Two stages, deliberately separable:

    SAP --CPI--> ADLS (JSONL)        app.ingest.fetch
    ADLS -------> Azure SQL          app.ingest.load

Splitting them is not ceremony. A failed load can be replayed against bytes
already on disk without asking SAP for them again; the landed files are an
audit of exactly what SAP returned on a given day, which matters when a number
is disputed weeks later; and the two halves fail for completely different
reasons -- one for network and service defects, the other for schema and
driver problems -- so keeping them apart keeps their errors legible.

This is the live counterpart to ``app.seed``, which loads delivered workbooks.
The two write to different table prefixes and neither knows about the other:

    app.seed    XLSX  ->  raw_<table>
    app.ingest  CPI   ->  odata_<table>

They cannot share a table. The workbook carries Excel headers ("Material
Number"), OData carries property names ("Matnr"), and the column sets differ by
an order of magnitude -- MARA has 244 columns in the extract against 7 over
OData. Reconciling the two vocabularies is the normalise step's job, not
something to fudge by letting whichever loader ran last decide the shape.
"""
