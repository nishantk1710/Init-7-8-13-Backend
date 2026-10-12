"""The I08 snapshot, built in its own process and stored in Azure SQL.

With ``I8_SNAPSHOT_STORE=sql`` the API no longer builds the I08 snapshot in its
web worker. A build is a separate process (``python -m
app.initiatives.i8.snapshot_store build``) that writes the finished snapshot to
``i8_snapshot_run``; the API loads the newest one into memory -- a few
megabytes, seconds to read -- and serves it exactly as before.

Unlike I13's store, the whole snapshot is one stored payload. I08's results
are small (thousands of repair lines and universe rows, not millions); its cost
was always the reading, which ``staged.py`` narrows and this package moves out
of the API. So every I08 route keeps reading the same in-memory ``Snapshot``.

Modules:

* ``codec``     -- the snapshot to compressed JSON and back
* ``store``     -- the run rows: save, newest ready, failures
* ``lifecycle`` -- start a build process from the API and wait for it
* ``__main__``  -- the build process itself
"""
