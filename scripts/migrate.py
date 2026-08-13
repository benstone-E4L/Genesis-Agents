"""CLI entrypoint for the Genesis schema migration runner.

This is the documented command. Render pre-deploy:

    python scripts/migrate.py

Boot/CI guard (exits 2 when the database is behind the repository):

    python scripts/migrate.py --check

The logic lives in ``migrations/runner.py``; this wrapper exists so the command
is stable even if the runner module moves, and so ``migrations/`` does not have
to become an importable package on the deploy path.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrations.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
