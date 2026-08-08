#!/usr/bin/env python
"""Regenerate runtime/prohibited_tools.sha256.

Running this is a deliberate act of weakening or extending the
PERMANENTLY_PROHIBITED list. The output file is named for what you are
changing so the diff cannot be mistaken for routine maintenance.

If you are running this to make a boot failure go away, stop and read
docs/FINANCE-TOOL-CONTRACTS.md Section 6 first.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.tool_policy import (  # noqa: E402
    PROHIBITION_MANIFEST_NAMES,
    prohibition_manifest_digest,
)

MANIFEST = ROOT / "runtime" / "prohibited_tools.sha256"


def main() -> int:
    digest = prohibition_manifest_digest()
    body = (
        "# Frozen manifest of the PERMANENTLY_PROHIBITED tool list.\n"
        "# docs/FINANCE-TOOL-CONTRACTS.md Section 6.2 Layer 3.\n"
        "#\n"
        "# runtime/tool_policy.assert_prohibitions_intact() recomputes this digest\n"
        "# at boot and REFUSES TO START the process on a mismatch. Editing the\n"
        "# prohibition list alone therefore breaks the boot: whoever weakens it\n"
        "# must also regenerate a hash in a file named for what they are weakening.\n"
        "#\n"
        f"# Covers {len(PROHIBITION_MANIFEST_NAMES)} names:\n"
        + "".join(f"#   {n}\n" for n in PROHIBITION_MANIFEST_NAMES)
        + "#\n"
        "# Regenerate with: python scripts/regen_prohibited_manifest.py\n"
        f"{digest}\n"
    )
    MANIFEST.write_text(body, encoding="utf-8")
    print(f"wrote {MANIFEST}")
    print(f"names={len(PROHIBITION_MANIFEST_NAMES)} digest={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
