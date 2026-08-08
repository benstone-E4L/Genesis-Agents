"""
Dynamic agent loader for the SwarmSync Agent Gateway.

This module is responsible for:
- Mapping marketplace slugs (e.g. "builder_agent") to real Python modules
  and classes/functions under the root-level `agents/` package.
- Safely importing and instantiating those agents without ever crashing
  the gateway process.

If anything goes wrong while importing or constructing an agent
 (missing package, ImportError, bad attribute, runtime error, etc.),
`load_agent()` returns None so the caller can gracefully fall back to the
LLM persona path.
"""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Path setup -----------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = PROJECT_ROOT / "agents"


def _ensure_import_paths() -> None:
    """
    Ensure that both the project root and the `agents/` directory are
    available on sys.path so imports like:

        import agents.builder_agent
        from infrastructure.x402_client import ...

    work correctly when running inside the agents-gateway app.
    """
    for path in (PROJECT_ROOT, AGENTS_DIR):
        try:
            str_path = str(path)
            if str_path not in sys.path:
                sys.path.insert(0, str_path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to add %s to sys.path: %s", path, exc, exc_info=True)


@dataclass(frozen=True)
class AgentTarget:
    """
    Describes how to load a particular agent.

    - module: dotted module path (e.g. "agents.builder_agent")
    - attr: name of class or factory function inside that module; if None,
      the module object itself is returned.
    """

    module: str
    attr: Optional[str]


# Slug-to-agent mapping ------------------------------------------------------

AGENT_REGISTRY: Dict[str, AgentTarget] = {
    # Meta / orchestration
    "genesis_meta_agent": AgentTarget("agents.genesis_meta_agent", "GenesisMetaAgent"),
    "business_idea_generator": AgentTarget("agents.business_idea_generator", "get_idea_generator"),
    # Core AP2-style agents
    "builder_agent": AgentTarget("agents.builder_agent", "BuilderAgent"),
    "builder_agent_enhanced": AgentTarget("agents.builder_agent_enhanced", "EnhancedBuilderAgent"),
    "deploy_agent": AgentTarget("agents.deploy_agent", "DeployAgent"),
    "qa_agent": AgentTarget("agents.qa_agent", "QAAgent"),
    "research_discovery_agent": AgentTarget("agents.research_discovery_agent", "ResearchDiscoveryAgent"),
    "spec_agent": AgentTarget("agents.spec_agent", "SpecAgent"),
    "security_agent": AgentTarget("agents.security_agent", "EnhancedSecurityAgent"),
    "maintenance_agent": AgentTarget("agents.maintenance_agent", "MaintenanceAgent"),
    "seo_agent": AgentTarget("agents.seo_agent", "SEOAgent"),
    "content_agent": AgentTarget("agents.content_agent", "ContentAgent"),
    "marketing_agent": AgentTarget("agents.marketing_agent", "MarketingAgent"),
    "support_agent": AgentTarget("agents.support_agent", "SupportAgent"),
    "analyst_agent": AgentTarget("agents.analyst_agent", "AnalystAgent"),
    "finance_agent": AgentTarget("agents.finance_agent", "FinanceAgent"),
    "pricing_agent": AgentTarget("agents.pricing_agent", "PricingAgent"),
    "email_agent": AgentTarget("agents.email_agent", "EmailAgent"),
    "billing_agent": AgentTarget("agents.billing_agent", "BillingAgent"),
    "commerce_agent": AgentTarget("agents.commerce_agent", "CommerceAgent"),
    "darwin_agent": AgentTarget("agents.darwin_agent", "DarwinAgent"),
    "domain_name_agent": AgentTarget("agents.domain_name_agent", "DomainNameAgent"),
    "legal_agent": AgentTarget("agents.legal_agent", "LegalAgent"),
    "onboarding_agent": AgentTarget("agents.onboarding_agent", "OnboardingAgent"),
    "reflection_agent": AgentTarget("agents.reflection_agent", "ReflectionAgent"),
    "se_darwin_agent": AgentTarget("agents.se_darwin_agent", "SEDarwinAgent"),
    "ring1t_reasoning_agent": AgentTarget("agents.ring1t_reasoning", "Ring1TReasoning"),
    # WaltzRL safety agents (wrapper modules expose factory functions)
    "waltzrl_conversation_agent": AgentTarget(
        "agents.waltzrl_conversation_agent",
        "get_waltzrl_conversation_agent",
    ),
    "waltzrl_feedback_agent": AgentTarget(
        "agents.waltzrl_feedback_agent",
        "get_waltzrl_feedback_agent",
    ),
    # Genesis x402 agents (FastAPI services; we only validate imports here)
    "genesis_research_x402": AgentTarget("agents.genesis_research_x402", None),
    "genesis_builder_x402": AgentTarget("agents.genesis_builder_x402", None),
    "genesis_deploy_x402": AgentTarget("agents.genesis_deploy_x402", None),
    "genesis_content_x402": AgentTarget("agents.genesis_content_x402", None),
    "genesis_email_x402": AgentTarget("agents.genesis_email_x402", None),
    "genesis_commerce_x402": AgentTarget("agents.genesis_commerce_x402", None),
    "genesis_qa_x402": AgentTarget("agents.genesis_qa_x402", None),
    "genesis_support_x402": AgentTarget("agents.genesis_support_x402", None),
    "genesis_finance_x402": AgentTarget("agents.genesis_finance_x402", None),
    "genesis_security_x402": AgentTarget("agents.genesis_security_x402", None),
    "genesis_billing_x402": AgentTarget("agents.genesis_billing_x402", None),
    "genesis_analyst_x402": AgentTarget("agents.genesis_analyst_x402", None),
    "genesis_marketing_x402": AgentTarget("agents.genesis_marketing_x402", None),
    "genesis_seo_x402": AgentTarget("agents.genesis_seo_x402", None),
    "genesis_meta_x402": AgentTarget("agents.genesis_meta_x402", None),
}


def _instantiate(attr: Any) -> Any:
    """
    Instantiate an agent given a loaded attribute.

    This supports both classes and factory functions. If the attribute is not
    callable, it is returned as-is.
    """
    if not callable(attr):
        return attr
    try:
        return attr()
    except TypeError:
        # Some factories may require arguments; in that case we return the
        # callable itself and let callers decide how to use it.
        logger.debug("Agent attribute requires arguments; returning callable directly")
        return attr


def load_agent(slug: str) -> Optional[Any]:
    """
    Try to load the Python agent backing a given slug.

    Returns:
        - An instantiated agent object, or a module/callable representing the
          agent, if everything imports correctly.
        - None if anything fails, so the caller can fall back to the Llama
          persona path without crashing the gateway.
    """
    target = AGENT_REGISTRY.get(slug)
    if not target:
        logger.info("No registered Python agent for slug '%s'", slug)
        return None

    _ensure_import_paths()

    try:
        module = importlib.import_module(target.module)
    except Exception as exc:
        logger.error(
            "Failed to import module '%s' for agent slug '%s': %s",
            target.module,
            slug,
            exc,
            exc_info=True,
        )
        return None

    if target.attr is None:
        # Caller will decide how to use the module (e.g. FastAPI app)
        return module

    try:
        attr = getattr(module, target.attr)
    except AttributeError as exc:
        logger.error(
            "Attribute '%s' not found in module '%s' for agent slug '%s': %s",
            target.attr,
            target.module,
            slug,
            exc,
            exc_info=True,
        )
        return None

    try:
        agent = _instantiate(attr)
    except Exception as exc:  # pragma: no cover - ultra-defensive
        logger.error(
            "Failed to construct agent for slug '%s' from %s.%s: %s",
            slug,
            target.module,
            target.attr,
            exc,
            exc_info=True,
        )
        return None

    return agent

