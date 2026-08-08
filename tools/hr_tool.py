"""HR agent tools - scaffolds for Greenhouse/Lever/BambooHR plus a functional template generator.

Phase 5 placeholders: ATS integrations defer to Phase 9 (OAuth/credential delegation).
hr_template_generate is fully functional and returns structured markdown templates.
"""
from __future__ import annotations
import logging
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)


_PHASE9_NOTE = "Phase 9 OAuth integration pending"


TEMPLATES: dict[str, str] = {
    "offer_letter": """# Offer Letter

Dear {candidate_name},

We are pleased to offer you the position of {role} at {company_name}, starting on {start_date}.

## Compensation
- Base salary: ${salary}
- Equity: {equity}
- Benefits: {benefits}

## Reporting
You will report to {manager_name}. Your primary work location will be {location}.

## Acceptance
Please sign below to accept this offer. This offer expires on {offer_expiry}.

Sincerely,
{signatory}
{signatory_title}
""",
    "policy_update": """# Policy Update: {policy_name}

Effective: {effective_date}

## Summary
{summary}

## Details
{details}

## Action Required
{action}

## Questions
Contact {contact_name} ({contact_email}) with any questions.
""",
    "onboarding_checklist": """# Onboarding Checklist: {employee_name}

Start date: {start_date}
Role: {role}
Manager: {manager_name}

## Day 1
- [ ] Welcome email sent
- [ ] Laptop and equipment delivered
- [ ] Accounts provisioned (email, Slack, {tools})
- [ ] Office tour / virtual intro
- [ ] Buddy assigned: {buddy_name}

## Week 1
- [ ] HR paperwork completed
- [ ] Benefits enrollment
- [ ] Manager 1:1 scheduled
- [ ] Team intro meeting
- [ ] Read team docs / runbooks

## Week 2-4
- [ ] First project assigned
- [ ] Training milestones: {training_milestones}
- [ ] 30-day check-in scheduled

## Day 90
- [ ] 90-day review
- [ ] Goals for next quarter set
""",
    "termination_letter": """# Termination Letter

Date: {date}

Dear {employee_name},

This letter confirms the termination of your employment with {company_name}, effective {termination_date}.

## Reason
{reason}

## Final Pay
Your final paycheck will include:
- Outstanding wages through {termination_date}
- Accrued but unused PTO: {pto_balance}
- {additional_pay}

## Benefits
{benefits_continuation}

## Return of Property
Please return all company property ({property_list}) by {return_date}.

## Confidentiality
You remain bound by the confidentiality and non-disclosure obligations in your employment agreement.

If you have any questions, contact {hr_contact}.

Sincerely,
{signatory}
{signatory_title}
""",
}


async def hr_greenhouse_query(*, query: str, scope: str = "candidates", **kwargs: Any) -> dict[str, Any]:
    try:
        return {
            "ok": False,
            "scaffold": True,
            "tool": "hr_greenhouse_query",
            "query": query,
            "scope": scope,
            "message": f"{_PHASE9_NOTE} with Greenhouse",
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def hr_lever_query(*, query: str, scope: str = "candidates", **kwargs: Any) -> dict[str, Any]:
    try:
        return {
            "ok": False,
            "scaffold": True,
            "tool": "hr_lever_query",
            "query": query,
            "scope": scope,
            "message": f"{_PHASE9_NOTE} with Lever",
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def hr_bamboohr_query(*, query: str, scope: str = "employees", **kwargs: Any) -> dict[str, Any]:
    try:
        return {
            "ok": False,
            "scaffold": True,
            "tool": "hr_bamboohr_query",
            "query": query,
            "scope": scope,
            "message": f"{_PHASE9_NOTE} with BambooHR",
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def hr_template_generate(
    *, template_type: str, context: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    try:
        if template_type not in TEMPLATES:
            return {
                "ok": False,
                "error": "unknown_template_type",
                "template_type": template_type,
                "available": list(TEMPLATES.keys()),
            }
        template = TEMPLATES[template_type]
        ctx = dict(context or {})

        # Identify the placeholders the buyer must fill.
        import string
        formatter = string.Formatter()
        placeholders = sorted({
            fname for _, fname, _, _ in formatter.parse(template) if fname
        })

        # Render with provided context; leave anything missing as a labeled placeholder.
        class _SafeDict(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        rendered = template.format_map(_SafeDict(ctx))

        missing = [p for p in placeholders if p not in ctx]
        return {
            "ok": True,
            "template_type": template_type,
            "rendered": rendered,
            "placeholders": placeholders,
            "missing_placeholders": missing,
            "notes": "Fill in any remaining {placeholders} before sending.",
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


HR_GREENHOUSE_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "hr_greenhouse_query",
        "description": "Query Greenhouse ATS for candidates, jobs, or applications. Scaffold pending Phase 9 OAuth.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query or API filter."},
                "scope": {
                    "type": "string",
                    "description": "Resource scope (candidates, jobs, applications).",
                    "default": "candidates",
                },
            },
            "required": ["query"],
        },
    },
}

HR_LEVER_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "hr_lever_query",
        "description": "Query Lever ATS for candidates, postings, or opportunities. Scaffold pending Phase 9 OAuth.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query or API filter."},
                "scope": {
                    "type": "string",
                    "description": "Resource scope (candidates, postings, opportunities).",
                    "default": "candidates",
                },
            },
            "required": ["query"],
        },
    },
}

HR_BAMBOOHR_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "hr_bamboohr_query",
        "description": "Query BambooHR for employee records, time off, or reports. Scaffold pending Phase 9 OAuth.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query or API filter."},
                "scope": {
                    "type": "string",
                    "description": "Resource scope (employees, time_off, reports).",
                    "default": "employees",
                },
            },
            "required": ["query"],
        },
    },
}

HR_TEMPLATE_GENERATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "hr_template_generate",
        "description": (
            "Generate a markdown HR document template. Supports offer_letter, policy_update, "
            "onboarding_checklist, termination_letter. Provided context values are substituted; "
            "any remaining placeholders are reported in missing_placeholders."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "template_type": {
                    "type": "string",
                    "enum": ["offer_letter", "policy_update", "onboarding_checklist", "termination_letter"],
                    "description": "Which HR template to render.",
                },
                "context": {
                    "type": "object",
                    "description": "Key/value substitutions for template placeholders.",
                    "additionalProperties": True,
                },
            },
            "required": ["template_type"],
        },
    },
}


def register() -> None:
    register_tool("hr_greenhouse_query", hr_greenhouse_query, HR_GREENHOUSE_QUERY_SCHEMA)
    register_tool("hr_lever_query", hr_lever_query, HR_LEVER_QUERY_SCHEMA)
    register_tool("hr_bamboohr_query", hr_bamboohr_query, HR_BAMBOOHR_QUERY_SCHEMA)
    register_tool("hr_template_generate", hr_template_generate, HR_TEMPLATE_GENERATE_SCHEMA)
