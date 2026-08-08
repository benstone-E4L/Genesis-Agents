"""Escrow containment for the Cato-facing / E4L deployment.

docs/FINANCE-TOOL-CONTRACTS.md Sections 3.8 and 6.3.

Why this module exists
----------------------
``escrow_client.py`` is not dormant. It has eleven live call sites in
``main.py`` and ``worker.py``. ``worker.py`` settles escrow with
``status="SETTLED"`` on job success with **no human in the loop at any point** —
automation paying, which is the exact inversion of the governing rule that
automation may PREPARE, a human PAYS and automation RECORDS.

All four escrow functions are PERMANENTLY_PROHIBITED (Section 6.1, items 16-19).
They are not registered tools, so the tool-registry prohibition layers do not
reach them. This module is their equivalent.

Deployment profiles
-------------------
``GENESIS_DEPLOYMENT_PROFILE``:

  unset (default)          Escrow calls are BLOCKED. Fail closed: an escrow path
                           that has not been explicitly claimed by an operator
                           does not run. Boot succeeds with a loud warning.
  ``cato``                 Escrow calls are BLOCKED **and** ``escrow_client``
                           must be absent from the deployment artefact. If the
                           module is importable, ``assert_escrow_containment()``
                           raises and the process refuses to start.
  ``swarmsync-marketplace``
                           Escrow calls are permitted. This is the SwarmSync
                           marketplace product, which owns this path. It must be
                           opted into explicitly; it is never the default.

Section 6.3 rule 5 is deliberate and repeated here: an unset ``INTERNAL_SECRET``
is the WEAKEST control and must never be the only one. That is why containment
lives in code and in the boot sequence rather than in an environment variable
that happens to be empty.
"""
from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

PROFILE_ENV_VAR = "GENESIS_DEPLOYMENT_PROFILE"
PROFILE_CATO = "cato"
PROFILE_MARKETPLACE = "swarmsync-marketplace"

ESCROW_FUNCTIONS: tuple[str, ...] = (
    "escrow_client.initiate_escrow",
    "escrow_client.complete_escrow",
    "escrow_client.release_escrow",
    "escrow_client.calculate_split",
)


def deployment_profile() -> str:
    """Read the profile at call time so tests and operators can change it."""
    return (os.getenv(PROFILE_ENV_VAR) or "").strip().lower()


def escrow_permitted() -> bool:
    """True only under an explicit swarmsync-marketplace profile.

    Fail closed by design: every other value, including unset, blocks. An
    over-restrictive result is correctable; an over-permissive one is not.
    """
    return deployment_profile() == PROFILE_MARKETPLACE


def is_cato_facing() -> bool:
    """True when this build has declared itself Cato-facing / E4L-relevant."""
    return deployment_profile() == PROFILE_CATO


def escrow_client_importable() -> bool:
    try:
        return importlib.util.find_spec("escrow_client") is not None
    except (ImportError, ValueError):
        return False


def assert_escrow_containment() -> None:
    """Boot assertion. Raises in a Cato-facing build that still ships escrow_client.

    Section 6.3 rule 2: the tolerant ``try/except ImportError`` fallback in
    main.py becomes the ONLY path, and the warning becomes a hard startup
    assertion that the module is absent.
    """
    if is_cato_facing() and escrow_client_importable():
        raise RuntimeError(
            "escrow_client is importable in a Cato-facing build. "
            f"{PROFILE_ENV_VAR}={PROFILE_CATO} requires that escrow_client.py is "
            "removed from the deployment artefact: it constructs and transmits "
            "funds movements with no human in the loop. "
            "See docs/FINANCE-TOOL-CONTRACTS.md Section 6.3."
        )
    if not escrow_permitted():
        log.warning(
            "escrow containment ACTIVE (%s=%r): all escrow operations are blocked. "
            "Set %s=%s only in the SwarmSync marketplace deployment.",
            PROFILE_ENV_VAR,
            deployment_profile() or "<unset>",
            PROFILE_ENV_VAR,
            PROFILE_MARKETPLACE,
        )


def escrow_blocked(operation: str) -> dict[str, Any]:
    """The Section 6.4 refusal envelope for a blocked escrow operation.

    Shaped so an existing call site that branches on ``.get("ok")`` sees a
    failure rather than a silent success. Emitted at critical severity because
    an escrow call reaching this function means a money path was attempted in a
    build that must not have one.
    """
    log.critical(
        "ESCROW OPERATION BLOCKED: %s (%s=%r). "
        "Automation may prepare; a human pays; automation records.",
        operation,
        PROFILE_ENV_VAR,
        deployment_profile() or "<unset>",
    )
    try:
        from tools._envelope import prohibited_refusal

        envelope = prohibited_refusal(f"escrow_client.{operation}", group="A")
    except Exception:  # pragma: no cover - envelope must never be the reason we fail open
        envelope = {
            "ok": False,
            "tool": f"escrow_client.{operation}",
            "error": {"code": "policy_denied", "retryable": False},
        }
    envelope["escrow_blocked"] = True
    return envelope


__all__ = [
    "PROFILE_ENV_VAR",
    "PROFILE_CATO",
    "PROFILE_MARKETPLACE",
    "ESCROW_FUNCTIONS",
    "deployment_profile",
    "escrow_permitted",
    "is_cato_facing",
    "escrow_client_importable",
    "assert_escrow_containment",
    "escrow_blocked",
]
