"""
Conduit-powered AP2 Escrow Verification

Implements the three-function interface required by the /verify endpoint:

  build_action_plan(spec)          -> list[dict]
  run_verification(session_id, spec, context) -> VerificationResult
  evaluate_result(result, spec)    -> tuple[bool, str]

The action plan follows the canonical Conduit sequence:
  NAVIGATE -> SCREENSHOT -> EXTRACT -> FINGERPRINT ->
  ACCESSIBILITY_SNAPSHOT -> EXPORT_PROOF

All browser operations use Playwright directly because the agents-gateway
is a Python service; we do not route through the NestJS Conduit API here.
Cost accounting is handled by the NestJS layer when it receives the callback.

Platform absorbs the verification cost (~8c per run, as documented in the
task spec). The client_context is injected into the EXPORT_PROOF bundle so
that the proof is cryptographically tied to the specific SwarmSync.AI escrow.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.request as _urllib_req
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import HTTPRedirectHandler

from verification_models import (
    VerificationActionLog,
    VerificationContext,
    VerificationResult,
    VerificationSpec,
)

# ---------------------------------------------------------------------------
# Load rubric engine from tools/rubric.py
# ---------------------------------------------------------------------------
_GATEWAY_ROOT = Path(__file__).resolve().parent
_rubric_path = _GATEWAY_ROOT / "conduit" / "tools" / "rubric.py"
if _rubric_path.exists():
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("gateway_rubric", _rubric_path)
    _rubric_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rubric_mod)
    evaluate_rubric = _rubric_mod.evaluate_rubric
    make_rubric_hash = _rubric_mod.make_rubric_hash
else:
    evaluate_rubric = None  # type: ignore[assignment]
    make_rubric_hash = None  # type: ignore[assignment]
    logger_init = logging.getLogger(__name__)
    logger_init.warning("conduit/tools/rubric.py not found — Track 2 rubric verification disabled")

logger = logging.getLogger(__name__)

# Action costs (cents) — mirrors the NestJS ACTION_COSTS_CENTS table so that
# the proof bundle can include accurate cost data in the audit trail.
_ACTION_COSTS: dict[str, int] = {
    "NAVIGATE": 1,
    "SCREENSHOT": 5,
    "EXTRACT": 2,
    "EVAL": 2,
    "FINGERPRINT": 1,
    "ACCESSIBILITY_SNAPSHOT": 2,
    "EXPORT_PROOF": 0,
}

# Maximum characters returned in an extracted text snippet
_EXTRACT_MAX_CHARS = int(os.getenv("CONDUIT_EXTRACT_MAX_CHARS", "5000"))

# HMAC secret for signing the proof bundle (same env var used by NestJS invoice signing)
_CONDUIT_INVOICE_SECRET = os.getenv("CONDUIT_INVOICE_SECRET", "")


# ---------------------------------------------------------------------------
# SSRF guard for server-side artifact fetch
# ---------------------------------------------------------------------------

class _SafeRedirectHandler(HTTPRedirectHandler):
    """Blocks redirects to private/loopback IPs."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _assert_safe_url(url: str) -> None:
    """Raise ValueError if url resolves to a private or loopback IP."""
    import ipaddress, socket
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"DNS resolution failed for host: {host!r}")
    for info in infos:
        addr_str = info[4][0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(
                f"Blocked: {url!r} resolved to private/loopback IP {addr_str}"
            )


def _fetch_and_hash(url: str) -> tuple[str, str | None]:
    """
    Fetch artifact bytes from url and return (sha256_hex, error_str).
    Uses streaming read (64 KB chunks) to handle large files.
    SSRF-safe: blocks private/loopback IPs and re-checks on every redirect.
    """
    try:
        _assert_safe_url(url)
    except ValueError as exc:
        return "", str(exc)
    try:
        opener = _urllib_req.build_opener(_SafeRedirectHandler)
        req = _urllib_req.Request(url, headers={"User-Agent": "ConduitVerify/2.0"})
        sha = hashlib.sha256()
        with opener.open(req, timeout=30) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest(), None
    except Exception as exc:
        return "", str(exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_action_plan(spec: VerificationSpec) -> list[dict[str, Any]]:
    """
    Convert a VerificationSpec into the ordered Conduit action sequence.

    The sequence is deterministic so that the audit trail is reproducible
    and tamper-evident.
    """
    plan: list[dict[str, Any]] = [
        {"type": "NAVIGATE", "url": spec.url},
        {"type": "SCREENSHOT"},
    ]

    if spec.selector:
        plan.append({"type": "EXTRACT", "selector": spec.selector})
    else:
        # Fall back to main-content extraction when no selector given
        plan.append({"type": "EXTRACT", "selector": "body"})

    plan.append({"type": "FINGERPRINT"})
    plan.append({"type": "ACCESSIBILITY_SNAPSHOT"})
    plan.append({"type": "EXPORT_PROOF"})

    return plan


async def run_verification(
    session_id: str,
    spec: VerificationSpec,
    context: VerificationContext,
) -> VerificationResult:
    """
    Execute the Conduit verification action plan against the deliverable URL or
    inline content.

    v2 verification tracks (run BEFORE browser actions):
      Track 1 — exact hash: fetch/hash artifact bytes, compare SHA-256
      Track 2 — rubric:     evaluate rubric predicates against artifact text

    Option C (inline_content set, url absent):
      Bytes are taken directly from spec.inline_content — no HTTP fetch, no
      browser session.  All v2 tracks apply identically to the inline bytes.
      A lightweight proof bundle is built without browser action entries.

    When a v2 track is supplied the browser session still runs (for proof
    bundle completeness) but `passed` and `verification_track` are determined
    by the v2 result.

    On any unrecoverable error a VerificationResult with passed=False is
    returned — exceptions are never surfaced to the caller.
    """
    # ------------------------------------------------------------------
    # Option C: resolve inline bytes (no URL required)
    # ------------------------------------------------------------------
    inline_mode = bool(spec.inline_content and not spec.url)
    inline_bytes: bytes | None = (
        spec.inline_content.encode("utf-8", errors="replace")
        if spec.inline_content
        else None
    )

    from patchright.async_api import async_playwright

    # ------------------------------------------------------------------
    # Track 1: exact hash verification
    # ------------------------------------------------------------------
    track1_hash: str | None = None
    track1_passed: bool | None = None
    track1_error: str | None = None

    if spec.expected_hash:
        if inline_mode:
            # Option C: hash inline bytes directly (url is absent)
            track1_hash = hashlib.sha256(inline_bytes).hexdigest()  # type: ignore[arg-type]
            track1_passed = track1_hash.lower() == spec.expected_hash.lower()
            if not track1_passed:
                track1_error = (
                    f"Hash mismatch: expected {spec.expected_hash[:16]}... "
                    f"got {track1_hash[:16]}..."
                )
        else:
            actual_hash, fetch_err = _fetch_and_hash(spec.url)  # type: ignore[arg-type]
            if fetch_err:
                track1_passed = False
                track1_error = f"Track 1 fetch failed: {fetch_err}"
            else:
                track1_hash = actual_hash
                track1_passed = actual_hash.lower() == spec.expected_hash.lower()
                if not track1_passed:
                    track1_error = (
                        f"Hash mismatch: expected {spec.expected_hash[:16]}... "
                        f"got {actual_hash[:16]}..."
                    )

    # ------------------------------------------------------------------
    # Track 2: rubric verification
    # ------------------------------------------------------------------
    track2_result: dict | None = None
    track2_passed: bool | None = None
    track2_error: str | None = None

    if spec.rubric_json and spec.rubric_hash:
        if make_rubric_hash is None or evaluate_rubric is None:
            track2_passed = False
            track2_error = "Track 2: rubric engine not available on this gateway"
        else:
            # Re-hash rubric to verify pre-commitment
            recomputed = make_rubric_hash(spec.rubric_json)
            if recomputed != spec.rubric_hash:
                track2_passed = False
                track2_error = (
                    f"Track 2: rubric_hash mismatch — "
                    f"expected {spec.rubric_hash[:16]}... got {recomputed[:16]}..."
                )
            else:
                try:
                    if inline_mode:
                        # Option C: evaluate inline content directly (url is absent)
                        content = inline_bytes.decode("utf-8", errors="replace")  # type: ignore[union-attr]
                    else:
                        _assert_safe_url(spec.url)  # type: ignore[arg-type]
                        opener = _urllib_req.build_opener(_SafeRedirectHandler)
                        req = _urllib_req.Request(
                            spec.url,  # type: ignore[arg-type]
                            headers={"User-Agent": "ConduitVerify/2.0"},
                        )
                        with opener.open(req, timeout=30) as resp:
                            content = resp.read().decode("utf-8", errors="replace")
                    track2_result = evaluate_rubric(content, spec.rubric_json)
                    track2_passed = track2_result["rubric_pass"]
                    if not track2_passed:
                        failed = [
                            p["predicate"]
                            for p in track2_result.get("predicate_results", [])
                            if not p["passed"]
                        ]
                        track2_error = f"Track 2: rubric failed predicates: {', '.join(failed)}"
                except Exception as exc:
                    track2_passed = False
                    track2_error = f"Track 2 fetch failed: {exc}"

    # ------------------------------------------------------------------
    # Determine v2 outcome before running browser (used later)
    # ------------------------------------------------------------------
    v2_track: int = 0
    v2_passed: bool | None = None
    v2_failure_reason: str | None = None

    if track1_passed is not None:
        v2_track = 1
        v2_passed = track1_passed
        v2_failure_reason = track1_error
    elif track2_passed is not None:
        v2_track = 2
        v2_passed = track2_passed
        v2_failure_reason = track2_error

    # ------------------------------------------------------------------
    # Browser session (skipped in Option C / inline mode)
    # ------------------------------------------------------------------
    action_log: list[VerificationActionLog] = []
    screenshot_b64: str | None = None
    extracted_content: str | None = None
    fingerprint_hash: str | None = None
    accessibility_text: str | None = None
    error_reason: str | None = None

    if inline_mode:
        # No browser needed — inline content is already in memory.
        # Populate extracted_content for proof bundle and legacy evaluate_result().
        extracted_content = (
            spec.inline_content[:_EXTRACT_MAX_CHARS] if spec.inline_content else None
        )
        # SHA-256 of inline bytes serves as the "fingerprint"
        if inline_bytes:
            fingerprint_hash = hashlib.sha256(inline_bytes).hexdigest()
        # Log a single synthetic INLINE_CONTENT action for the audit trail
        action_log.append(
            VerificationActionLog(
                index=0,
                action="INLINE_CONTENT",
                url=None,
                selector=None,
                success=True,
                cost_cents=0,
                duration_ms=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
                data_snippet=f"[inline {len(inline_bytes or b'')} bytes]",
            )
        )
    else:
        action_plan = build_action_plan(spec)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = await browser.new_page()

            try:
                for idx, action in enumerate(action_plan):
                    action_type = action["type"]
                    cost_cents = _ACTION_COSTS.get(action_type, 0)
                    t_start = time.monotonic()
                    success = False
                    data_snippet: str | None = None

                    try:
                        if action_type == "NAVIGATE":
                            await page.goto(
                                action["url"],
                                wait_until="networkidle",
                                timeout=30_000,
                            )
                            success = True

                        elif action_type == "SCREENSHOT":
                            raw_bytes = await page.screenshot(full_page=False)
                            screenshot_b64 = base64.b64encode(raw_bytes).decode()
                            data_snippet = f"[screenshot {len(raw_bytes)} bytes]"
                            success = True

                        elif action_type == "EXTRACT":
                            selector = action.get("selector", "body")
                            try:
                                element = await page.query_selector(selector)
                                if element:
                                    text = await element.inner_text()
                                    extracted_content = text[:_EXTRACT_MAX_CHARS]
                                    data_snippet = extracted_content[:200]
                                    success = True
                                else:
                                    # Selector not found — not a fatal error; mark as soft fail
                                    extracted_content = None
                                    data_snippet = f"[selector not found: {selector}]"
                                    success = False
                            except Exception:
                                extracted_content = None
                                success = False

                        elif action_type == "FINGERPRINT":
                            content = await page.content()
                            fingerprint_hash = hashlib.sha256(
                                content.encode("utf-8", errors="replace")
                            ).hexdigest()
                            data_snippet = fingerprint_hash[:16]
                            success = True

                        elif action_type == "ACCESSIBILITY_SNAPSHOT":
                            try:
                                snapshot = await page.accessibility.snapshot()
                                accessibility_text = json.dumps(snapshot, ensure_ascii=False)[:_EXTRACT_MAX_CHARS]
                                data_snippet = "[a11y snapshot captured]"
                                success = True
                            except Exception:
                                accessibility_text = None
                                success = False

                        elif action_type == "EVAL":
                            script = action.get("script", "")
                            eval_args = action.get("args", {})
                            try:
                                # page.evaluate runs JS in Chromium context and returns the result.
                                # The script is wrapped in an async IIFE so it can use await internally
                                # (e.g. fetch + crypto.subtle.digest).
                                eval_result = await page.evaluate(
                                    f"(async (args) => {{ {script} }})(args)",
                                    eval_args,
                                )
                                data_snippet = str(eval_result)[:200]
                                success = True
                                # Store result on the action dict so callers can read it back
                                action["_eval_result"] = eval_result
                            except Exception as e:
                                data_snippet = f"EVAL_ERROR: {e}"
                                success = False

                        elif action_type == "EXPORT_PROOF":
                            # EXPORT_PROOF is free and always succeeds —
                            # the bundle is assembled below after the loop.
                            success = True
                            data_snippet = "[proof bundle assembled]"

                    except Exception as exc:
                        logger.warning(
                            "Conduit action %s failed in session %s: %s",
                            action_type,
                            session_id,
                            exc,
                        )
                        success = False
                        if error_reason is None:
                            error_reason = f"{action_type}: {exc}"

                    duration_ms = int((time.monotonic() - t_start) * 1000)

                    action_log.append(
                        VerificationActionLog(
                            index=idx,
                            action=action_type,
                            url=action.get("url"),
                            selector=action.get("selector"),
                            success=success,
                            cost_cents=cost_cents,
                            duration_ms=duration_ms,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            data_snippet=data_snippet,
                        )
                    )

            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Determine final pass/fail
    # v2 track result takes precedence; fall back to legacy content check
    # ------------------------------------------------------------------
    if v2_passed is not None:
        passed = v2_passed
        failure_reason = v2_failure_reason
    else:
        passed, failure_reason = evaluate_result(
            {
                "extracted_content": extracted_content,
                "fingerprint_hash": fingerprint_hash,
                "action_log": [al.model_dump() for al in action_log],
            },
            spec,
        )
        if not passed and error_reason and not failure_reason:
            failure_reason = error_reason

    # Build the proof bundle — this is the tamper-evident record
    client_context_dict = context.model_dump()
    proof_bundle = _build_proof_bundle(
        session_id=session_id,
        spec=spec,
        context=client_context_dict,
        extracted_content=extracted_content,
        fingerprint_hash=fingerprint_hash,
        accessibility_text=accessibility_text,
        action_log=[al.model_dump() for al in action_log],
        passed=passed,
        failure_reason=failure_reason,
        verification_track=v2_track,
        eval_result_hash=track1_hash,
        rubric_result=track2_result,
    )

    proof_bundle_json = json.dumps(proof_bundle, ensure_ascii=False, sort_keys=True)
    proof_hash = hashlib.sha256(proof_bundle_json.encode()).hexdigest()

    # Sign the bundle: HMAC-SHA256 over the serialised proof
    sig = _sign_proof(proof_bundle_json)

    # Store the bundle as base64 ref (no external storage in gateway)
    proof_bundle_ref = base64.b64encode(proof_bundle_json.encode()).decode()

    # Screenshot ref: embed base64 inline (small, no external storage needed)
    screenshot_ref = (
        f"data:image/png;base64,{screenshot_b64}" if screenshot_b64 else None
    )

    return VerificationResult(
        passed=passed,
        proof_hash=proof_hash,
        proof_bundle_ref=proof_bundle_ref,
        conduit_session_sig=sig,
        extracted_content=extracted_content,
        screenshot_ref=screenshot_ref,
        failure_reason=failure_reason,
        action_log=action_log,
        client_context=client_context_dict,
        eval_result_hash=track1_hash,
        verification_track=v2_track if v2_track else None,
        request_id=spec.request_id,
        rubric_result=track2_result,
    )


def evaluate_result(
    result: dict[str, Any],
    spec: VerificationSpec,
) -> tuple[bool, str]:
    """
    Compare the extracted content against the expected content in the spec.

    Rules:
    1. If expectedContent is set: the extracted text must contain it
       (case-insensitive substring match).
    2. If no expectedContent: pass if NAVIGATE and FINGERPRINT both succeeded.
    3. If the NAVIGATE action failed: always fail.

    Returns (passed: bool, reason: str).
    """
    action_log: list[dict[str, Any]] = result.get("action_log", [])

    # Check NAVIGATE succeeded (skipped for inline-content jobs)
    navigate_actions = [a for a in action_log if a.get("action") == "NAVIGATE"]
    inline_actions = [a for a in action_log if a.get("action") == "INLINE_CONTENT"]

    if inline_actions:
        # Option C path: no NAVIGATE, content came from inline bytes
        pass
    elif navigate_actions and not navigate_actions[0].get("success", False):
        return False, "NAVIGATE action failed — URL may be inaccessible"
    elif not navigate_actions:
        return False, "No NAVIGATE action found in audit trail"

    extracted: str | None = result.get("extracted_content")
    expected: str | None = spec.expectedContent

    if expected:
        if extracted is None:
            return False, "Content extraction returned no data — cannot verify expected content"
        if expected.lower() not in extracted.lower():
            snippet = (extracted[:200] + "...") if len(extracted) > 200 else extracted
            return (
                False,
                f"Expected content not found. Expected: '{expected[:100]}'. "
                f"Extracted snippet: '{snippet}'",
            )
        return True, "Expected content found in extracted page content"

    # No expected content specified — verify page loaded (fingerprint present)
    fingerprint_hash = result.get("fingerprint_hash")
    if fingerprint_hash:
        return True, "Page loaded and fingerprinted successfully"

    fingerprint_actions = [a for a in action_log if a.get("action") == "FINGERPRINT"]
    if fingerprint_actions and fingerprint_actions[0].get("success", False):
        return True, "Page fingerprinted successfully"

    return (
        False,
        "Page did not load or could not be fingerprinted",
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_proof_bundle(
    *,
    session_id: str,
    spec: VerificationSpec,
    context: dict[str, Any],
    extracted_content: str | None,
    fingerprint_hash: str | None,
    accessibility_text: str | None,
    action_log: list[dict[str, Any]],
    passed: bool,
    failure_reason: str | None,
    verification_track: int = 0,
    eval_result_hash: str | None = None,
    rubric_result: dict | None = None,
) -> dict[str, Any]:
    """
    Build the tamper-evident proof bundle.

    The header contains the client_context (marketplace, escrow ref, etc.) so
    that the bundle is tied to a specific SwarmSync.AI escrow transaction.

    When context.marketplace == "SwarmSync.AI" a branded text receipt header
    is included in the bundle metadata.
    """
    is_swarmsync = context.get("marketplace") == "SwarmSync.AI"
    receipt_header: str | None = None

    if is_swarmsync:
        receipt_header = (
            "\n"
            + "=" * 47 + "\n"
            + "  CONDUIT VERIFICATION RECEIPT\n"
            + "  Powered by Conduit · Requested by SwarmSync.AI\n"
            + "  Purpose: Escrow Verification\n"
            + f"  Escrow: {context.get('escrow_ref', 'unknown')}\n"
            + "=" * 47
        )

    total_cost_cents = sum(a.get("cost_cents", 0) for a in action_log)

    return {
        "header": {
            "version": "1.0",
            "generator": "SwarmSync.AI Agent Gateway",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "client_context": context,
            "receipt": receipt_header,
        },
        "spec": {
            "url": spec.url,
            "selector": spec.selector,
            "expectedContent": spec.expectedContent,
            "fingerprintDelta": spec.fingerprintDelta,
            "delivery_mode": "inline" if (spec.inline_content and not spec.url) else "url",
            "inline_content_length": len(spec.inline_content) if spec.inline_content else None,
        },
        "result": {
            "passed": passed,
            "failure_reason": failure_reason,
            "verification_track": verification_track,
            "eval_result_hash": eval_result_hash,
            "rubric_result": rubric_result,
            "extracted_content": extracted_content,
            "fingerprint_hash": fingerprint_hash,
            "accessibility_snapshot_length": len(accessibility_text) if accessibility_text else 0,
        },
        "audit": {
            "action_count": len(action_log),
            "total_cost_cents": total_cost_cents,
            "actions": action_log,
        },
    }


def _sign_proof(proof_bundle_json: str) -> str:
    """
    HMAC-SHA256 sign the serialised proof bundle.

    Falls back to a SHA-256 content hash when CONDUIT_INVOICE_SECRET is not
    configured (dev-only; production must always have the secret set).
    """
    secret = _CONDUIT_INVOICE_SECRET
    if not secret:
        logger.warning(
            "CONDUIT_INVOICE_SECRET not set — using unsigned SHA-256 digest for proof sig"
        )
        return "sha256:" + hashlib.sha256(proof_bundle_json.encode()).hexdigest()

    return hmac.new(
        secret.encode(),
        proof_bundle_json.encode(),
        hashlib.sha256,
    ).hexdigest()
