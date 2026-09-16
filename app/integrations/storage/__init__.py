"""Storage adapters.

One module per backing platform, each implementing ``app.core.storage.Storage``.
Application code never imports an adapter directly -- it calls ``get_storage()``,
which selects one from ``STORAGE_URL``. Importing an adapter by name reintroduces
the environment branch the URL scheme exists to remove.

    local.py   local filesystem -- stands in for cloud storage during development

The Azure Data Lake adapter belongs in ``app/integrations/azure/`` alongside the
other Azure services, and is registered in ``app.core.storage.build_storage``.
"""
