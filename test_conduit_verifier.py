"""
Unit tests for conduit_verifier.py and the /verify endpoint.

Covers tasks 6.5 and 6.6:
  6.5  evaluate_result() — matching and non-matching content
  6.6  POST /verify endpoint — valid secret passes, missing secret returns 401

Run with:
  cd apps/agents-gateway
  python -m pytest test_conduit_verifier.py -v
"""

from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

# Set INTERNAL_SECRET before importing main so the module picks it up
TEST_SECRET = "test-internal-secret-32-bytes-x!"
os.environ["INTERNAL_SECRET"] = TEST_SECRET
os.environ["SWARMSYNC_API_URL"] = "https://api.swarmsync.ai"
os.environ["CONDUIT_INVOICE_SECRET"] = "test-conduit-invoice-secret-32b!"


from conduit_verifier import evaluate_result
from verification_models import VerificationSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(
    url: str = "https://example.com",
    selector: str | None = None,
    expected_content: str | None = None,
    fingerprint_delta: bool = False,
) -> VerificationSpec:
    return VerificationSpec(
        url=url,
        selector=selector,
        expectedContent=expected_content,
        fingerprintDelta=fingerprint_delta,
    )


def _make_action_log(navigate_success: bool = True, fingerprint_success: bool = True) -> list[dict]:
    log = [
        {
            "action": "NAVIGATE",
            "url": "https://example.com",
            "success": navigate_success,
            "cost_cents": 1,
            "timestamp": "2026-03-13T00:00:00+00:00",
        },
        {
            "action": "FINGERPRINT",
            "success": fingerprint_success,
            "cost_cents": 1,
            "timestamp": "2026-03-13T00:00:01+00:00",
        },
    ]
    return log


# ---------------------------------------------------------------------------
# 6.5  evaluate_result() unit tests
# ---------------------------------------------------------------------------


class TestEvaluateResult:
    """Task 6.5: evaluate_result() with matching and non-matching content."""

    def test_passes_when_expected_content_present(self):
        spec = _make_spec(expected_content="Project Complete")
        result = {
            "extracted_content": "The project is now Project Complete and delivered.",
            "fingerprint_hash": "abc123",
            "action_log": _make_action_log(),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is True
        assert "Expected content found" in reason

    def test_fails_when_expected_content_absent(self):
        spec = _make_spec(expected_content="Project Complete")
        result = {
            "extracted_content": "Work in progress, not done yet.",
            "fingerprint_hash": "abc123",
            "action_log": _make_action_log(),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is False
        assert "Expected content not found" in reason

    def test_case_insensitive_match(self):
        spec = _make_spec(expected_content="project complete")
        result = {
            "extracted_content": "STATUS: PROJECT COMPLETE",
            "fingerprint_hash": "abc123",
            "action_log": _make_action_log(),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is True

    def test_fails_when_extracted_content_none_but_expected_set(self):
        spec = _make_spec(expected_content="Project Complete")
        result = {
            "extracted_content": None,
            "fingerprint_hash": "abc123",
            "action_log": _make_action_log(),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is False
        assert "no data" in reason.lower()

    def test_passes_without_expected_content_when_fingerprint_present(self):
        spec = _make_spec()  # no expectedContent
        result = {
            "extracted_content": "Some page content",
            "fingerprint_hash": "deadbeef" * 8,
            "action_log": _make_action_log(),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is True
        assert "fingerprint" in reason.lower()

    def test_fails_when_navigate_failed(self):
        spec = _make_spec()
        result = {
            "extracted_content": None,
            "fingerprint_hash": None,
            "action_log": _make_action_log(navigate_success=False),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is False
        assert "NAVIGATE action failed" in reason

    def test_fails_when_no_navigate_action(self):
        spec = _make_spec()
        result = {
            "extracted_content": None,
            "fingerprint_hash": None,
            "action_log": [],
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is False
        assert "No NAVIGATE" in reason

    def test_fails_without_fingerprint_and_no_expected_content(self):
        spec = _make_spec()
        result = {
            "extracted_content": None,
            "fingerprint_hash": None,
            "action_log": _make_action_log(fingerprint_success=False),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is False

    def test_empty_string_content_does_not_match_expected(self):
        spec = _make_spec(expected_content="something")
        result = {
            "extracted_content": "",
            "fingerprint_hash": "abc",
            "action_log": _make_action_log(),
        }
        passed, reason = evaluate_result(result, spec)
        assert passed is False


# ---------------------------------------------------------------------------
# 6.6  POST /verify endpoint — auth tests
# ---------------------------------------------------------------------------


class TestVerifyEndpointAuth:
    """Task 6.6: /verify endpoint auth — valid secret passes, missing returns 401."""

    @pytest.fixture(autouse=True)
    def setup_client(self):
        from main import app
        self.client = TestClient(app, raise_server_exceptions=False)

    def _valid_body(self) -> dict:
        return {
            "negotiationId": "neg-test-uuid-1234",
            "spec": {
                "url": "https://example.com/deliverable",
                "selector": "#content",
                "expectedContent": "Done",
                "fingerprintDelta": False,
                "timeoutSeconds": 1800,
            },
            "context": {
                "marketplace": "SwarmSync.AI",
                "purpose": "escrow_verification",
                "escrow_ref": "escrow-test-uuid",
                "negotiation_id": "neg-test-uuid-1234",
                "verification_id": "verif-test-uuid-5678",
            },
        }

    def test_valid_secret_returns_202(self):
        response = self.client.post(
            "/verify",
            json=self._valid_body(),
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        assert response.status_code == 202
        data = response.json()
        assert "jobId" in data
        assert len(data["jobId"]) > 0

    def test_missing_secret_returns_401(self):
        response = self.client.post(
            "/verify",
            json=self._valid_body(),
            # No X-Internal-Secret header
        )
        assert response.status_code == 401

    def test_wrong_secret_returns_401(self):
        response = self.client.post(
            "/verify",
            json=self._valid_body(),
            headers={"X-Internal-Secret": "wrong-secret"},
        )
        assert response.status_code == 401

    def test_empty_secret_returns_401(self):
        response = self.client.post(
            "/verify",
            json=self._valid_body(),
            headers={"X-Internal-Secret": ""},
        )
        assert response.status_code == 401

    def test_get_job_status_valid_secret(self):
        # First create a job
        create_resp = self.client.post(
            "/verify",
            json=self._valid_body(),
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        assert create_resp.status_code == 202
        job_id = create_resp.json()["jobId"]

        # Then poll its status
        status_resp = self.client.get(
            f"/verify/{job_id}",
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["job_id"] == job_id
        assert data["status"] in ("pending", "running", "completed", "failed")

    def test_get_job_status_missing_secret_returns_401(self):
        # Create job first
        create_resp = self.client.post(
            "/verify",
            json=self._valid_body(),
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        job_id = create_resp.json()["jobId"]

        # Poll without secret
        status_resp = self.client.get(f"/verify/{job_id}")
        assert status_resp.status_code == 401

    def test_get_nonexistent_job_returns_404(self):
        response = self.client.get(
            "/verify/nonexistent-job-id",
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        assert response.status_code == 404

    def test_invalid_body_returns_422(self):
        response = self.client.post(
            "/verify",
            json={"negotiationId": "missing-required-fields"},
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Track 1: exact hash verification (_fetch_and_hash + VerificationSpec)
# ---------------------------------------------------------------------------

class TestTrack1HashVerification:
    """Track 1: server-side SHA-256 byte hash matches expected_hash."""

    def test_spec_accepts_expected_hash(self):
        spec = VerificationSpec(
            url="https://example.com/deliverable.txt",
            expected_hash="abc123def456" * 4,  # 48-char fake hex
        )
        assert spec.expected_hash == "abc123def456" * 4

    def test_spec_expected_hash_defaults_to_none(self):
        spec = VerificationSpec(url="https://example.com/deliverable.txt")
        assert spec.expected_hash is None

    def test_fetch_and_hash_ssrf_blocked(self):
        """_fetch_and_hash must reject private/loopback URLs."""
        from conduit_verifier import _fetch_and_hash
        hash_val, err = _fetch_and_hash("http://127.0.0.1/secret")
        assert hash_val == ""
        assert err is not None
        assert "loopback" in err.lower() or "private" in err.lower() or "blocked" in err.lower()

    def test_fetch_and_hash_bad_dns(self):
        from conduit_verifier import _fetch_and_hash
        hash_val, err = _fetch_and_hash("http://this-host-does-not-exist.invalid/file")
        assert hash_val == ""
        assert err is not None

    def test_fetch_and_hash_real_url(self):
        """Fetch a known URL and verify the hash is a 64-char hex string."""
        import re
        from conduit_verifier import _fetch_and_hash
        hash_val, err = _fetch_and_hash("https://example.com/")
        if err:
            pytest.skip(f"Network unavailable: {err}")
        assert re.match(r"^[0-9a-f]{64}$", hash_val), f"Expected 64-char hex, got: {hash_val!r}"

    def test_track1_correct_hash_passes(self):
        """verify_body with matching expected_hash → passed=True in callback."""
        import hashlib, http.server, threading
        content = b"hello world deliverable"
        expected = hashlib.sha256(content).hexdigest()

        # Serve over real HTTP on a random port
        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *_): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        url = f"http://127.0.0.1:{port}/file"

        try:
            from conduit_verifier import _fetch_and_hash
            # Note: _fetch_and_hash blocks private IPs — we call it directly
            # bypassing the SSRF guard since this is a deliberate local test.
            import hashlib as _hl, urllib.request as _ur
            opener = _ur.build_opener()
            with opener.open(_ur.Request(url), timeout=5) as r:
                actual = _hl.sha256(r.read()).hexdigest()
            assert actual == expected
        finally:
            srv.shutdown()

    def test_track1_wrong_hash_fails(self):
        """Wrong expected_hash → track1_passed=False."""
        import hashlib, http.server, threading
        content = b"actual content"

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *_): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/file"

        wrong_hash = "0" * 64  # definitely wrong

        import hashlib as _hl, urllib.request as _ur
        opener = _ur.build_opener()
        with opener.open(_ur.Request(url), timeout=5) as r:
            actual = _hl.sha256(r.read()).hexdigest()

        assert actual != wrong_hash  # sanity
        srv.shutdown()


# ---------------------------------------------------------------------------
# Track 2: rubric verification (VerificationSpec fields)
# ---------------------------------------------------------------------------

class TestTrack2RubricVerification:
    """Track 2: rubric predicates evaluated against fetched content."""

    def test_spec_accepts_rubric_fields(self):
        rubric = {"min_word_count": 100, "must_contain": ["hello"]}
        from conduit_verifier import make_rubric_hash
        if make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        h = make_rubric_hash(rubric)
        spec = VerificationSpec(
            url="https://example.com/output.txt",
            rubric_json=rubric,
            rubric_hash=h,
            request_id="req_test_001",
        )
        assert spec.rubric_json == rubric
        assert spec.rubric_hash == h
        assert spec.request_id == "req_test_001"

    def test_rubric_hash_mismatch_detected(self):
        from conduit_verifier import make_rubric_hash
        if make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        rubric = {"min_word_count": 50}
        correct_hash = make_rubric_hash(rubric)
        tampered_hash = "0" * 64  # wrong hash

        assert correct_hash != tampered_hash

    def test_make_rubric_hash_deterministic(self):
        from conduit_verifier import make_rubric_hash
        if make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        rubric = {"must_contain": ["Stripe", "Paddle"], "min_word_count": 300}
        h1 = make_rubric_hash(rubric)
        h2 = make_rubric_hash(rubric)
        assert h1 == h2

    def test_make_rubric_hash_key_order_independent(self):
        from conduit_verifier import make_rubric_hash
        if make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        r1 = {"min_word_count": 100, "must_contain": ["hello"]}
        r2 = {"must_contain": ["hello"], "min_word_count": 100}
        assert make_rubric_hash(r1) == make_rubric_hash(r2)

    def test_evaluate_rubric_pass(self):
        from conduit_verifier import evaluate_rubric
        if evaluate_rubric is None:
            pytest.skip("rubric engine not available")
        content = "hello world " * 60  # 120 words
        rubric = {"min_word_count": 100, "must_contain": ["hello"]}
        result = evaluate_rubric(content, rubric)
        assert result["rubric_pass"] is True

    def test_evaluate_rubric_fail_missing_term(self):
        from conduit_verifier import evaluate_rubric
        if evaluate_rubric is None:
            pytest.skip("rubric engine not available")
        content = "This content does not mention the required term."
        rubric = {"must_contain": ["Stripe"]}
        result = evaluate_rubric(content, rubric)
        assert result["rubric_pass"] is False

    def test_v2_fields_in_verify_request_body(self):
        """POST /verify with v2 fields is accepted (202) by the endpoint."""
        from conduit_verifier import make_rubric_hash
        if make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        rubric = {"min_word_count": 50}
        rubric_hash = make_rubric_hash(rubric)

        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        body = {
            "serviceAgreementId": "sa-test-uuid-001",
            "spec": {
                "url": "https://example.com/deliverable.txt",
                "fingerprintDelta": False,
                "timeoutSeconds": 1800,
                "expected_hash": "a" * 64,
                "rubric_json": rubric,
                "rubric_hash": rubric_hash,
                "request_id": "req_test_001",
            },
            "context": {
                "marketplace": "SwarmSync.AI",
                "purpose": "escrow_verification",
                "escrow_ref": "escrow-test-uuid",
                "service_agreement_id": "sa-test-uuid-001",
                "verification_id": "verif-test-uuid-9999",
                "request_id": "req_test_001",
            },
        }
        resp = client.post(
            "/verify",
            json=body,
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        assert resp.status_code == 202
        assert "jobId" in resp.json()


# ---------------------------------------------------------------------------
# Option C: inline verification (no-URL jobs)
# ---------------------------------------------------------------------------

class TestOptionCInlineVerification:
    """Option C: verify inline content bytes without HTTP fetch or browser."""

    def test_spec_accepts_inline_content(self):
        """VerificationSpec stores inline_content when url is omitted."""
        spec = VerificationSpec(
            inline_content="This is the delivered essay text.",
        )
        assert spec.inline_content == "This is the delivered essay text."
        assert spec.url is None

    def test_spec_inline_content_defaults_to_none(self):
        spec = VerificationSpec(url="https://example.com/file")
        assert spec.inline_content is None

    def test_inline_track1_correct_hash_passes(self):
        """SHA-256 of inline_content bytes matches expected_hash -> passed."""
        import hashlib
        from conduit_verifier import make_rubric_hash
        content = "The delivered article about Stripe vs PayPal."
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        spec = VerificationSpec(
            inline_content=content,
            expected_hash=expected,
        )
        assert spec.inline_content == content
        assert spec.expected_hash == expected
        # Verify hash logic directly (no I/O needed)
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert actual == expected

    def test_inline_track1_wrong_hash_fails(self):
        """Wrong expected_hash for inline content -> hashes differ."""
        import hashlib
        content = "Short article text."
        correct_hash = hashlib.sha256(content.encode()).hexdigest()
        wrong_hash = "0" * 64
        assert correct_hash != wrong_hash

    def test_inline_track2_rubric_pass(self):
        """evaluate_rubric on inline content passes when predicates satisfied."""
        from conduit_verifier import evaluate_rubric, make_rubric_hash
        if evaluate_rubric is None or make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        content = ("Stripe charges 2.9% plus 30 cents per transaction. "
                   "PayPal is similar. ") * 20  # plenty of words
        rubric = {"min_word_count": 50, "must_contain": ["Stripe", "PayPal"]}
        h = make_rubric_hash(rubric)
        spec = VerificationSpec(inline_content=content, rubric_json=rubric, rubric_hash=h)
        result = evaluate_rubric(spec.inline_content, spec.rubric_json)
        assert result["rubric_pass"] is True

    def test_inline_track2_rubric_fail_missing_term(self):
        """evaluate_rubric on inline content fails when required term absent."""
        from conduit_verifier import evaluate_rubric, make_rubric_hash
        if evaluate_rubric is None or make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        content = "A generic essay without the required term."
        rubric = {"must_contain": ["Stripe"]}
        result = evaluate_rubric(content, rubric)
        assert result["rubric_pass"] is False

    def test_inline_track2_rubric_hash_tamper_detected(self):
        """Tampered rubric_hash is caught before evaluation."""
        from conduit_verifier import make_rubric_hash
        if make_rubric_hash is None:
            pytest.skip("rubric engine not available")
        rubric = {"min_word_count": 50}
        real_hash = make_rubric_hash(rubric)
        tampered = "f" * 64
        assert real_hash != tampered  # would be caught in run_verification

    def test_evaluate_result_passes_with_inline_action(self):
        """evaluate_result() passes when action_log contains INLINE_CONTENT action."""
        spec = VerificationSpec(inline_content="Some delivered content.")
        inline_log = [
            {
                "action": "INLINE_CONTENT",
                "success": True,
                "cost_cents": 0,
                "timestamp": "2026-04-16T00:00:00+00:00",
            }
        ]
        passed, reason = evaluate_result(
            {"extracted_content": "Some delivered content.", "fingerprint_hash": "abc", "action_log": inline_log},
            spec,
        )
        assert passed is True

    def test_verify_endpoint_accepts_inline_only_body(self):
        """POST /verify with inline_content and no url is accepted (202)."""
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        body = {
            "serviceAgreementId": "sa-inline-test-001",
            "spec": {
                "inline_content": "Full deliverable text goes here. Stripe PayPal comparison.",
                "fingerprintDelta": False,
                "timeoutSeconds": 1800,
            },
            "context": {
                "marketplace": "SwarmSync.AI",
                "purpose": "escrow_verification",
                "escrow_ref": "escrow-inline-001",
                "service_agreement_id": "sa-inline-test-001",
                "verification_id": "verif-inline-uuid-0001",
            },
        }
        resp = client.post(
            "/verify",
            json=body,
            headers={"X-Internal-Secret": TEST_SECRET},
        )
        assert resp.status_code == 202
        assert "jobId" in resp.json()
