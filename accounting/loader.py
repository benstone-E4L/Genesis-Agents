"""Load specialist contracts and entity packs. YAML is the source of book facts."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from accounting.specialists import (
    CONTRACT_TO_SLUG,
    ENTITY_KEYS,
    SLUG_TO_CONTRACT,
    is_e4l_specialist,
)

ACCOUNTING_DIR = Path(__file__).resolve().parent
CONTRACTS_DIR = ACCOUNTING_DIR / "contracts"
ENTITIES_DIR = ACCOUNTING_DIR / "entities"


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"accounting yaml missing: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        raise ValueError(f"empty accounting yaml: {path}")
    return data


@lru_cache(maxsize=32)
def load_contract(contract_id: str) -> dict[str, Any]:
    key = (contract_id or "").strip().replace(".yaml", "")
    if key.startswith("genesis-e4l-"):
        key = SLUG_TO_CONTRACT.get(key, "")
    if key not in CONTRACT_TO_SLUG:
        raise ValueError(f"unknown specialist contract: {contract_id!r}")
    data = _read_yaml(CONTRACTS_DIR / f"{key}.yaml")
    if not isinstance(data, dict):
        raise ValueError(f"contract is not a mapping: {key}")
    if data.get("id") != key:
        raise ValueError(f"contract id mismatch: file={key} id={data.get('id')!r}")
    expected_slug = CONTRACT_TO_SLUG[key]
    if data.get("bundle") != expected_slug:
        raise ValueError(
            f"contract {key} bundle={data.get('bundle')!r} expected {expected_slug}"
        )
    return data


@lru_cache(maxsize=32)
def load_contract_for_slug(slug: str) -> dict[str, Any]:
    if not is_e4l_specialist(slug):
        raise ValueError(f"not an E4L specialist slug: {slug!r}")
    return load_contract(SLUG_TO_CONTRACT[slug])


@lru_cache(maxsize=16)
def load_entity(entity_key: str) -> dict[str, Any]:
    key = (entity_key or "").strip()
    if key not in ENTITY_KEYS:
        raise ValueError(f"unknown entity pack (not an agent): {entity_key!r}")
    data = _read_yaml(ENTITIES_DIR / f"{key}.yaml")
    if not isinstance(data, dict) or data.get("key") != key:
        raise ValueError(f"entity pack mismatch: {key}")
    return data


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
