"""
Pydantic models for the Conduit-powered AP2 escrow verification system.

These models are shared between the /verify endpoint (main.py) and the
core verification logic (conduit_verifier.py).
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, model_validator


class VerificationSpec(BaseModel):
    """
    Specification for what to verify on a page, supplied by the delivering agent
    via verificationHints in the AP2 ServiceDeliveryDto.

    Either `url` or `inline_content` must be present.  When only `inline_content`
    is supplied (Option C — no-URL jobs), the gateway verifies those bytes directly
    without an HTTP fetch or browser session.
    """

    url: Optional[str] = Field(None, description="URL of the deliverable to verify")
    selector: Optional[str] = Field(
        None,
        description="CSS selector to extract content from for comparison",
    )
    expectedContent: Optional[str] = Field(
        None,
        description="Expected text content that must be present in the extracted element",
    )
    fingerprintDelta: bool = Field(
        False,
        description="If true, check the page fingerprint changed since last known state",
    )
    timeoutSeconds: int = Field(
        1800,
        ge=30,
        le=3600,
        description="Max seconds the verification job may run before TIMEOUT",
    )
    # ---------------------------------------------------------------------------
    # v2 verification fields
    # ---------------------------------------------------------------------------
    expected_hash: Optional[str] = Field(
        None,
        description="Track 1: exact SHA-256 hex of the expected artifact bytes",
    )
    rubric_json: Optional[dict] = Field(
        None,
        description="Track 2: generative task rubric predicates (min_word_count, must_contain, etc.)",
    )
    rubric_hash: Optional[str] = Field(
        None,
        description="Track 2: SHA-256 of json.dumps(rubric_json, sort_keys=True), pre-committed by buyer",
    )
    request_id: Optional[str] = Field(
        None,
        description="Buyer request/order ID — echoed in the callback for escrow correlation",
    )
    # ---------------------------------------------------------------------------
    # Option C: inline verification (no-URL jobs)
    # ---------------------------------------------------------------------------
    inline_content: Optional[str] = Field(
        None,
        description=(
            "Option C — full deliverable text supplied inline by the seller. "
            "When set and url is absent, the gateway hashes/rubric-evaluates "
            "these bytes directly without an HTTP fetch or browser session."
        ),
    )

    @model_validator(mode="after")
    def require_url_or_inline_content(self) -> "VerificationSpec":
        if not self.url and not self.inline_content:
            raise ValueError(
                "VerificationSpec requires either 'url' or 'inline_content'"
            )
        return self


class VerificationContext(BaseModel):
    """
    Context injected into the Conduit proof bundle header.
    Must appear verbatim in the EXPORT_PROOF payload.
    """

    marketplace: str = "SwarmSync.AI"
    purpose: str = "escrow_verification"
    escrow_ref: str
    negotiation_id: Optional[str] = None
    service_agreement_id: Optional[str] = None
    verification_id: str
    request_id: Optional[str] = None


class VerifyRequest(BaseModel):
    """Body for POST /verify."""

    negotiationId: Optional[str] = None
    serviceAgreementId: Optional[str] = None
    spec: VerificationSpec
    context: VerificationContext


class VerificationActionLog(BaseModel):
    """Single entry in the Conduit action audit trail."""

    index: int
    action: str
    url: Optional[str] = None
    selector: Optional[str] = None
    success: bool
    cost_cents: int
    duration_ms: Optional[int] = None
    timestamp: str
    data_snippet: Optional[str] = None


class VerificationResult(BaseModel):
    """
    Final result returned to the NestJS callback endpoint.
    The proof_bundle_ref points to the full tamper-evident bundle
    (e.g. an in-memory JSON blob stringified and stored as a base64 ref).
    """

    passed: bool
    proof_hash: str
    proof_bundle_ref: Optional[str] = None
    conduit_session_sig: str
    extracted_content: Optional[str] = None
    screenshot_ref: Optional[str] = None
    failure_reason: Optional[str] = None
    action_log: list[VerificationActionLog] = Field(default_factory=list)
    client_context: Optional[dict[str, Any]] = None
    # v2 fields
    eval_result_hash: Optional[str] = Field(
        None,
        description="Track 1: SHA-256 of fetched artifact bytes",
    )
    verification_track: Optional[int] = Field(
        None,
        description="1 = exact hash match, 2 = rubric predicate evaluation, 0 = legacy",
    )
    request_id: Optional[str] = Field(
        None,
        description="Buyer request/order ID echoed from spec for escrow correlation",
    )
    rubric_result: Optional[dict[str, Any]] = Field(
        None,
        description="Track 2: full evaluate_rubric() output dict when rubric verification ran",
    )


class VerifyJobStatus(BaseModel):
    """Response body for GET /verify/:job_id."""

    job_id: str
    status: str  # "pending" | "running" | "completed" | "failed"
    result: Optional[VerificationResult] = None
    created_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None


class ArbitrageVerificationRequest(BaseModel):
    """Body for POST /internal/arbitrage/verification-jobs."""

    verification_run_id: str
    transaction_id: str
    proposal_id: str
    method: Optional[str] = None
    policy: dict[str, Any] = Field(default_factory=dict)
    request_payload: dict[str, Any] = Field(default_factory=dict)


class ArbitrageVerificationResult(BaseModel):
    """Callback body for the Nest arbitrage verification endpoint."""

    verificationRunId: str
    transactionId: str
    status: str
    passed: bool
    score: Optional[float] = None
    failureReason: Optional[str] = None
    method: Optional[str] = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    evidenceUrls: list[str] = Field(default_factory=list)
    resultPayload: dict[str, Any] = Field(default_factory=dict)
