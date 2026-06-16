#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def default_task_contract() -> dict[str, Any]:
    return {
        "version": 1,
        "non_negotiable_constraints": [],
        "forbidden_substitutions": [],
        "required_live_evidence": [],
        "acceptance_criteria": [],
        "review_gates": [],
        "completion_policy": {
            "require_all_required_live_evidence_passed": True,
            "require_forbidden_substitutions_absent": True,
            "require_mandatory_constraints_reported": True,
        },
    }


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _normalize_bullet_text(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    match = re.match(r"^[-*]\s+(.*)$", stripped)
    if match:
        value = match.group(1).strip()
        return value or None
    checkbox = re.match(r"^- \[(?: |x|X)\]\s+(.*)$", stripped)
    if checkbox:
        value = checkbox.group(1).strip()
        return value or None
    return None


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower())
    normalized = normalized.strip("_")
    return normalized or "evidence"


def parse_task_markdown_contract(task_md: Path) -> dict[str, Any]:
    contract = default_task_contract()
    if not task_md.exists():
        return contract

    lines = task_md.read_text(encoding="utf-8").splitlines()
    current_section: str | None = None
    current_evidence_item: dict[str, Any] | None = None

    def flush_evidence_item() -> None:
        nonlocal current_evidence_item
        if current_evidence_item is None:
            return
        description = str(current_evidence_item.get("description", "")).strip()
        if description:
            contract["required_live_evidence"].append(current_evidence_item)
        current_evidence_item = None

    for raw_line in lines:
        line = raw_line.rstrip()
        heading = re.match(r"^(#{2,3})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            lowered = title.lower()
            if level == 2:
                flush_evidence_item()
                current_section = lowered
            elif current_section == "required verification" and level == 3:
                flush_evidence_item()
                current_evidence_item = {
                    "id": _slugify(title),
                    "description": title,
                    "required": True,
                }
            continue

        bullet_text = _normalize_bullet_text(line)
        if not bullet_text:
            continue

        if current_section == "hard constraints":
            contract["non_negotiable_constraints"].append(bullet_text)
            reject_match = re.match(r"^Reject if (.*)$", bullet_text, re.IGNORECASE)
            if reject_match:
                contract["forbidden_substitutions"].append(reject_match.group(1).strip())
        elif current_section == "review gates":
            contract["review_gates"].append(bullet_text)
            reject_match = re.match(r"^Reject if (.*)$", bullet_text, re.IGNORECASE)
            if reject_match:
                contract["forbidden_substitutions"].append(reject_match.group(1).strip())
        elif current_section == "acceptance criteria":
            contract["acceptance_criteria"].append(bullet_text)
        elif current_section == "required verification":
            if current_evidence_item is None:
                current_evidence_item = {
                    "id": _slugify(bullet_text),
                    "description": bullet_text,
                    "required": True,
                }
            else:
                description = str(current_evidence_item.get("description", "")).strip()
                current_evidence_item["description"] = f"{description}; {bullet_text}"

    flush_evidence_item()
    return contract


def merge_task_contract(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = default_task_contract()
    for key in ("non_negotiable_constraints", "forbidden_substitutions", "acceptance_criteria", "review_gates"):
        merged: list[str] = []
        for source in (base.get(key, []), override.get(key, [])):
            if isinstance(source, list):
                for item in source:
                    value = str(item).strip()
                    if value and value not in merged:
                        merged.append(value)
        result[key] = merged

    evidence_by_id: dict[str, dict[str, Any]] = {}
    for source in (base.get("required_live_evidence", []), override.get("required_live_evidence", [])):
        if not isinstance(source, list):
            continue
        for raw_item in source:
            if not isinstance(raw_item, dict):
                continue
            description = str(raw_item.get("description", "")).strip()
            item_id = str(raw_item.get("id", "")).strip() or _slugify(description)
            if not description:
                description = item_id
            merged_item = {
                "id": item_id,
                "description": description,
                "required": bool(raw_item.get("required", True)),
            }
            evidence_by_id[item_id] = merged_item
    result["required_live_evidence"] = list(evidence_by_id.values())

    result["completion_policy"] = dict(default_task_contract()["completion_policy"])
    for source in (base.get("completion_policy", {}), override.get("completion_policy", {})):
        if isinstance(source, dict):
            result["completion_policy"].update(source)

    version = override.get("version", base.get("version", 1))
    result["version"] = int(version) if str(version).isdigit() else 1
    return result


def load_task_contract(task_dir: Path) -> dict[str, Any]:
    task_dir = task_dir.resolve()
    task_md = task_dir / "task.md"
    file_contract = read_json(task_dir / "task_contract.json")
    markdown_contract = parse_task_markdown_contract(task_md)
    return merge_task_contract(markdown_contract, file_contract)


def ensure_task_contract_file(task_dir: Path) -> Path:
    task_dir = task_dir.resolve()
    path = task_dir / "task_contract.json"
    if path.exists():
        return path
    write_json(path, parse_task_markdown_contract(task_dir / "task.md"))
    return path


def render_task_contract_overlay(contract: dict[str, Any]) -> str:
    lines: list[str] = ["=== TASK EXECUTION CONTRACT ==="]

    non_negotiable = contract.get("non_negotiable_constraints", [])
    if isinstance(non_negotiable, list) and non_negotiable:
        lines.append("Non-negotiable constraints:")
        lines.extend(f"- {item}" for item in non_negotiable if str(item).strip())

    forbidden = contract.get("forbidden_substitutions", [])
    if isinstance(forbidden, list) and forbidden:
        lines.append("Forbidden substitutions:")
        lines.extend(f"- {item}" for item in forbidden if str(item).strip())

    required_evidence = contract.get("required_live_evidence", [])
    if isinstance(required_evidence, list) and required_evidence:
        lines.append("Required live evidence:")
        for item in required_evidence:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            description = str(item.get("description", "")).strip()
            required = bool(item.get("required", True))
            requirement_label = "required" if required else "optional"
            rendered = description or item_id
            lines.append(f"- [{requirement_label}] {item_id}: {rendered}")

    acceptance = contract.get("acceptance_criteria", [])
    if isinstance(acceptance, list) and acceptance:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {item}" for item in acceptance if str(item).strip())

    review_gates = contract.get("review_gates", [])
    if isinstance(review_gates, list) and review_gates:
        lines.append("Review gates:")
        lines.extend(f"- {item}" for item in review_gates if str(item).strip())

    lines.extend(
        [
            "Completion rule:",
            "- Do not mark the task approved or completed if any required live evidence is missing, blocked, or failed.",
            "- Do not mark the task approved or completed if a forbidden substitution is present.",
        ]
    )
    return "\n".join(lines)

