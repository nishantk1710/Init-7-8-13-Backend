"""``python -m app.initiatives.i8`` -- alias for the views CLI.

The views are the only thing in I08 with a command line; everything else is
served over HTTP. Keeping one entry point here means nobody has to remember
which submodule it lives in.
"""

from app.initiatives.i8.views import main

raise SystemExit(main())
