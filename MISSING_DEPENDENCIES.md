# Missing Dependencies for Python Agents

This file lists external dependencies used by the Python agents under `agents/`
that are **not** present in `apps/agents-gateway/requirements.txt`. Agents
that rely on these packages may fail to import or run inside the gateway and
will therefore fall back to the Llama persona path.

For each entry:

- **agent file**: path under the `agents/` directory
- **missing import**: top-level import that is not from the Python standard
  library and not listed in the gateway requirements
- **notes**: likely pip package name if obvious, or `custom - needs investigation`
  when unclear or internal.

---

- **agent file**: `agents/builder_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: custom Microsoft Agent Framework; likely internal / private SDK — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: probably provided by pip package `azure-identity`
  - missing import: `anthropic` (used for LLM client inside the file)
    - notes: pip package `anthropic`

- **agent file**: `agents/builder_agent_enhanced.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/qa_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`
  - missing import: `anthropic` (LLM client for OpenEnv E2E testing)
    - notes: pip package `anthropic`
  - missing import: `DeepSeekOCRCompressor` and related types from `infrastructure.deepseek_ocr_compressor`
    - notes: underlying DeepSeek OCR client likely requires additional third‑party packages — **custom - needs investigation**

- **agent file**: `agents/deploy_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`
  - missing import: `backoff`
    - notes: pip package `backoff`
  - missing import: `requests`
    - notes: pip package `requests`

- **agent file**: `agents/spec_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/security_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/maintenance_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/content_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`
  - missing import: `GenesisMemoryOSMongoDB`, `create_genesis_memory_mongodb` from `infrastructure.memory_os_mongodb_adapter`
    - notes: underlying MemoryOS MongoDB client likely depends on MongoDB drivers (`pymongo` or similar) — **custom - needs investigation**

- **agent file**: `agents/marketing_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/email_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/analyst_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/finance_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/pricing_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/billing_agent.py`
  - missing import: `agent_framework`, `agent_framework.azure`, `agent_framework.observability`
    - notes: Microsoft Agent Framework — **custom - needs investigation**
  - missing import: `azure.identity.aio`
    - notes: pip package `azure-identity`

- **agent file**: `agents/business_idea_generator.py`
  - missing import: `anthropic`
    - notes: pip package `anthropic`
  - missing import: `openai`
    - notes: pip package `openai`

- **agent file**: `agents/genesis_meta_agent.py`
  - direct top-level imports are from the standard library and in-repo `infrastructure` modules.
  - note: the `infrastructure.*` modules themselves depend on additional packages (e.g. `requests`, `fastapi`, MongoDB drivers, VOIX tooling), but those dependencies are tracked in their respective modules rather than here.

- **agent files**: `agents/commerce_agent.py`, `agents/domain_name_agent.py`, `agents/darwin_agent.py`, `agents/se_darwin_agent.py`, `agents/reflection_agent.py`, `agents/onboarding_agent.py`, `agents/legal_agent.py`, `agents/support_agent.py`, `agents/research_discovery_agent.py`
  - direct imports at the top of these files are limited to the Python standard library and in-repo `infrastructure` modules.
  - no additional third‑party packages were detected beyond what the infrastructure layer itself requires.

- **genesis x402 agent files**:
  - `agents/genesis_research_x402.py`
  - `agents/genesis_builder_x402.py`
  - `agents/genesis_deploy_x402.py`
  - `agents/genesis_content_x402.py`
  - `agents/genesis_email_x402.py`
  - `agents/genesis_commerce_x402.py`
  - `agents/genesis_qa_x402.py`
  - `agents/genesis_support_x402.py`
  - `agents/genesis_finance_x402.py`
  - `agents/genesis_security_x402.py`
  - `agents/genesis_billing_x402.py`
  - `agents/genesis_analyst_x402.py`
  - `agents/genesis_marketing_x402.py`
  - `agents/genesis_seo_x402.py`
  - `agents/genesis_meta_x402.py`
  - missing import: `fastapi`, `pydantic` (already covered in gateway requirements)
  - no additional third‑party imports beyond those and in-repo `infrastructure` modules were detected.

> Agents listed above can be imported and wired through the gateway, but **will
> fall back to the Llama persona path on Render unless the corresponding
> packages are installed into the agents-gateway environment.**
