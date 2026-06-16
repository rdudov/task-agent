#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_notify import try_send_pipeline_status_message, try_send_pipeline_stop_message
from task_contract import load_task_contract, render_task_contract_overlay


DEFAULT_CODEX_MODEL = os.environ.get("CODEX_MULTI_AGENT_DEFAULT_MODEL", "gpt-5.5")
CODEX_MODEL_FALLBACKS = tuple(
    model
    for model in os.environ.get("CODEX_MULTI_AGENT_MODEL_FALLBACKS", "gpt-5.5,gpt-5.4-mini,gpt-5.4").split(",")
    if model
)

ROLE_MODELS_CODEX = {
    "analyst": DEFAULT_CODEX_MODEL,
    "tz_reviewer": DEFAULT_CODEX_MODEL,
    "architect": DEFAULT_CODEX_MODEL,
    "architecture_reviewer": DEFAULT_CODEX_MODEL,
    "planner": DEFAULT_CODEX_MODEL,
    "plan_reviewer": DEFAULT_CODEX_MODEL,
    "developer": DEFAULT_CODEX_MODEL,
    "code_reviewer": DEFAULT_CODEX_MODEL,
}
ROLE_MODELS = ROLE_MODELS_CODEX

ROLE_MODELS_AGENT = {
    "analyst": "composer-2.5",
    "tz_reviewer": "composer-2.5",
    "architect": "composer-2.5",
    "architecture_reviewer": "composer-2.5",
    "planner": "composer-2.5",
    "plan_reviewer": "composer-2.5",
    "developer": "composer-2.5",
    "code_reviewer": "composer-2.5",
}

PIPELINE_RUNNERS = ("codex", "agent")

DEFAULT_AGENTS_REPO_URL = ""
REQUIRED_AGENT_PROMPTS = [
    "02_analyst_prompt.md",
    "03_tz_reviewer_prompt.md",
    "04_architect_prompt.md",
    "05_architecture_reviewer_prompt.md",
    "06_agent_planner.md",
    "07_agent_plan_reviewer.md",
    "08_agent_developer.md",
    "09_agent_code_reviewer.md",
]


@dataclass
class RoleResult:
    stdout: str
    parsed_json: dict[str, Any] | None


CODEX_ENV_BLOCKLIST = {
    "CODEX_THREAD_ID",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_task_dir(task_path: str) -> Path:
    path = Path(task_path)
    if not path.is_absolute():
        path = repo_root() / path
    return path.resolve()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def append_trace(task_dir: Path, message: str) -> None:
    trace_file = task_dir / "trace.md"
    if not trace_file.exists():
        write_text(trace_file, "# Trace\n\n")
    append_text(trace_file, f"- {utc_now()} {message}\n")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(read_text(path))


def write_status(task_dir: Path, state: str, current_step: str, extra: dict[str, Any] | None = None) -> None:
    status_file = task_dir / "status.json"
    payload = read_json(status_file)
    payload.update(
        {
            "state": state,
            "current_step": current_step,
            "updated_at": utc_now(),
            "workflow": "multi-agent-dev",
        }
    )
    if extra:
        payload.update(extra)
    write_json(status_file, payload)


def init_pipeline_status(path: Path) -> None:
    if path.exists():
        return
    write_text(
        path,
        "# Pipeline Status\n\n"
        f"Last Updated: {utc_now()}\n\n"
        "## Stages\n"
        "- [ ] Analysis\n"
        "- [ ] Architecture\n"
        "- [ ] Planning\n"
        "- [ ] Development\n\n"
        "## Notes\n"
        f"- [{utc_now()}] Pipeline initialized.\n",
    )


def read_pipeline_stage_status(path: Path) -> dict[str, str]:
    statuses = {
        "Analysis": "pending",
        "Architecture": "pending",
        "Planning": "pending",
        "Development": "pending",
    }
    if not path.exists():
        return statuses
    for line in read_text(path).splitlines():
        match = re.match(r"- \[(?:x|!| )\] (Analysis|Architecture|Planning|Development) — (approved|blocked|pending)", line)
        if match:
            statuses[match.group(1)] = match.group(2)
    return statuses


def read_pipeline_notes(path: Path) -> list[str]:
    if not path.exists():
        return []
    notes: list[str] = []
    in_notes = False
    for line in read_text(path).splitlines():
        if line.strip() == "## Notes":
            in_notes = True
            continue
        if not in_notes:
            continue
        if line.startswith("## "):
            break
        if line.startswith("- "):
            notes.append(line[2:])
    return notes


def note(message: str) -> str:
    return f"[{utc_now()}] {message}"


def update_pipeline_status(
    path: Path,
    analysis: str,
    architecture: str,
    planning: str,
    development: str,
    notes: list[str],
) -> None:
    def fmt(name: str, value: str) -> str:
        marker = "[x]" if value == "approved" else "[!]" if value == "blocked" else "[ ]"
        return f"- {marker} {name} — {value}"

    note_lines = "\n".join(f"- {note}" for note in notes) if notes else "- none"
    write_text(
        path,
        "# Pipeline Status\n\n"
        f"Last Updated: {utc_now()}\n\n"
        "## Stages\n"
        f"{fmt('Analysis', analysis)}\n"
        f"{fmt('Architecture', architecture)}\n"
        f"{fmt('Planning', planning)}\n"
        f"{fmt('Development', development)}\n\n"
        "## Notes\n"
        f"{note_lines}\n",
    )


def extract_json_block(text: str) -> dict[str, Any] | None:
    fence_matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = list(reversed(fence_matches))
    if not candidates:
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates = [stripped]

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def child_codex_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in CODEX_ENV_BLOCKLIST:
        env.pop(key, None)
    return env


def ensure_agents_dir(agents_dir: Path, repo_url: str, task_dir: Path | None = None) -> Path:
    """Ensure the external role-prompt repository exists and has the expected files."""

    agents_dir = agents_dir.resolve()
    if not agents_dir.exists():
        if not repo_url:
            raise SystemExit(
                "Agents prompt directory is missing and no repository URL is configured. "
                "Set --agents-repo-url or CODEX_MULTI_AGENT_PROMPTS_REPO."
            )
        agents_dir.parent.mkdir(parents=True, exist_ok=True)
        message = f"Agents prompt directory missing; cloning {repo_url} into {agents_dir}."
        if task_dir is not None:
            append_trace(task_dir, message)
        completed = subprocess.run(
            ["git", "clone", repo_url, str(agents_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
            raise SystemExit(output or f"Failed to clone agents prompt repository from {repo_url}")

    missing = [name for name in REQUIRED_AGENT_PROMPTS if not (agents_dir / name).is_file()]
    if missing:
        raise SystemExit(
            "Agents prompt directory is incomplete: "
            f"{agents_dir}. Missing: {', '.join(missing)}"
        )
    return agents_dir


def role_workdir(role_name: str, sandbox_mode: str) -> Path:
    if sandbox_mode == "danger-full-access" and role_name in {"developer", "code_reviewer"}:
        return Path("/opt/projects")
    return repo_root()


def role_models_for(runner: str) -> dict[str, str]:
    if runner == "agent":
        return ROLE_MODELS_AGENT
    if runner == "codex":
        return ROLE_MODELS_CODEX
    raise ValueError(f"Unsupported pipeline runner: {runner}")


def is_unsupported_model_error(output: str) -> bool:
    normalized = output.lower()
    return (
        "model is not supported" in normalized
        or "model' model is not supported" in normalized
        or ("not supported" in normalized and "model" in normalized)
    )


def model_attempts(model: str | None) -> list[str | None]:
    attempts: list[str | None] = []
    for candidate in [model or DEFAULT_CODEX_MODEL, *CODEX_MODEL_FALLBACKS]:
        if candidate not in attempts:
            attempts.append(candidate)
    return attempts


def run_codex_once(prompt: str, model: str | None, sandbox_mode: str, workdir: Path) -> tuple[int, str]:
    command = [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox_mode,
        "-C",
        str(workdir),
    ]
    if model:
        command.extend(["--model", model])
    command.append(prompt)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=child_codex_env(),
        cwd=workdir,
    )
    output = completed.stdout
    if completed.stderr:
        output = f"{output}\n{completed.stderr}".strip()
    return completed.returncode, output


def run_codex(prompt: str, model: str | None, sandbox_mode: str, workdir: Path) -> str:
    unsupported_failures: list[str] = []
    attempts = model_attempts(model)
    for index, candidate in enumerate(attempts):
        returncode, output = run_codex_once(prompt, candidate, sandbox_mode, workdir)
        if returncode == 0:
            if unsupported_failures:
                attempted = ", ".join(attempts[: index + 1])
                return f"Model fallback applied after unsupported model error. Attempts: {attempted}\n\n{output}"
            return output
        if is_unsupported_model_error(output) and index + 1 < len(attempts):
            unsupported_failures.append(output or f"codex exec failed with exit code {returncode}")
            continue
        raise RuntimeError(output or f"codex exec failed with exit code {returncode}")

    raise RuntimeError("\n\n".join(unsupported_failures) or "codex exec failed before selecting a supported model")


def run_agent(prompt: str, model: str | None, sandbox_mode: str, workdir: Path) -> str:
    command = [
        "agent",
        "--print",
        "--trust",
        "--force",
        "--workspace",
        str(workdir),
    ]
    if model:
        command.extend(["--model", model])
    if sandbox_mode == "read-only":
        command.extend(["--mode", "plan"])
    elif sandbox_mode == "danger-full-access":
        command.extend(["--sandbox", "disabled"])
    command.append(prompt)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=workdir,
    )
    output = completed.stdout
    if completed.stderr:
        output = f"{output}\n{completed.stderr}".strip()
    if completed.returncode != 0:
        raise RuntimeError(output or f"agent exec failed with exit code {completed.returncode}")
    return output


def run_child_agent(
    runner: str,
    prompt: str,
    model: str | None,
    sandbox_mode: str,
    workdir: Path,
) -> str:
    if runner == "agent":
        return run_agent(prompt, model, sandbox_mode, workdir)
    if runner == "codex":
        return run_codex(prompt, model, sandbox_mode, workdir)
    raise ValueError(f"Unsupported pipeline runner: {runner}")


def build_role_prompt(
    role_prompt: str,
    role_name: str,
    task_dir: Path,
    artifacts_dir: Path,
    task_file: Path,
    extra_inputs: list[str],
    runner: str,
) -> str:
    rel_task_dir = task_dir.relative_to(repo_root())
    rel_artifacts_dir = artifacts_dir.relative_to(repo_root())
    rel_task_file = task_file.relative_to(repo_root())
    rendered_role_prompt = role_prompt.replace("{artifacts_dir}", str(rel_artifacts_dir))
    inputs = "\n".join(f"- {item}" for item in extra_inputs if item)
    if not inputs:
        inputs = "- none"

    task_contract = load_task_contract(task_dir)
    contract_overlay = render_task_contract_overlay(task_contract)

    cli_label = "CURSOR AGENT CLI" if runner == "agent" else "CODEX CLI"
    return (
        f"{rendered_role_prompt}\n\n"
        f"=== ADDITIONAL EXECUTION INSTRUCTIONS FOR {cli_label} ===\n"
        f"Role: {role_name}\n"
        f"Pipeline runner: {runner}\n"
        f"Repository root: {repo_root()}\n"
        f"Primary task artifact: `{rel_task_file}`\n"
        f"Task directory: `{rel_task_dir}`\n"
        f"Pipeline artifacts directory: `{rel_artifacts_dir}`\n"
        "Use the repository files directly. Do not ask the user for confirmation unless a true blocker remains.\n"
        "Preserve the repository task contract: task-level progress stays in the task directory, and pipeline artifacts stay inside the pipeline artifacts directory.\n"
        "Read these project-level files before writing conclusions when relevant:\n"
        "- `AGENTS.md`\n"
        "- `README.md`\n"
        "- `docs/architecture.md`\n"
        "- `docs/task-execution.md`\n"
        "Return the required JSON block exactly as requested by the role prompt whenever that prompt requires JSON.\n"
        "If unresolved architecture or product-contract decisions remain, return them in `blocking_questions` and treat the stage as blocked instead of burying them as non-blocking notes.\n"
        "Typical blocking examples include failure-mode semantics, fallback policy, backward-compatibility expectations, migration scope, rollout source-of-truth, and whether legacy branches should continue to work.\n"
        "Do not invent backward compatibility, legacy fallback, or migration behavior unless the task or repository contract states it explicitly.\n"
        "Preserve the semantic target of the request. If the task names a reference behavior, artifact, provider, model, protocol feature, or runtime branch, implement and verify that named path directly instead of substituting a nearby effect unless the user or task contract explicitly accepts the substitution.\n"
        "If you create or update any pipeline artifact files, place them under the pipeline artifacts directory unless the role prompt explicitly requires repository code or documentation changes.\n"
        "If required verification is blocked only because project dependencies or test tools are missing, install them using the target project's standard dependency workflow and continue.\n"
        "Do not treat missing packages, absent test runners, or an unprepared project environment as a blocker until you have attempted that normal setup step.\n"
        "Do not treat tests around fixtures, mocks, helper functions, or test-only harnesses as sufficient proof that the real application works.\n"
        "For substantial implementation work, preserve at least one no-mock end-to-end verification path for the primary application or service behavior.\n"
        "If the task changes a service, API, daemon, worker, or other runtime process, include a smoke check against the real runtime entrypoint in its target launch mode before considering the task complete.\n"
        "Do not assume a single happy-path smoke validates every production branch. If behavior diverges by threshold, mode, provider, credential, feature flag, model variant, transport, or fallback logic, each production-relevant branch touched by the task needs explicit validation evidence or an explicitly documented verification gap.\n"
        "If a production-reachable branch is covered only by mocks, fake models, stubs, or test-only harnesses, treat that as insufficient verification and call it out.\n"
        "Across unit, integration, regression, and acceptance checks, prefer test data that is representative of the real inputs relevant to the behavior under test.\n"
        "Degenerate or trivial fixtures used only to satisfy the mechanics of a request are weak evidence when more realistic samples are practical.\n"
        "For media-oriented flows such as voice, audio, images, documents, or uploads, prefer realistic samples with substantive content over ultra-short placeholder fixtures. Validate duration-dependent, mode-dependent, and fallback branches explicitly when they are production-relevant.\n"
        "If you change git-tracked source in a repository with a configured remote, leave the finished change committed and pushed after verification unless the task explicitly requires local-only work or push is blocked. If push is not performed, record the reason and current repository state in the task artifacts and final output.\n"
        f"{contract_overlay}\n"
        "Inputs for this role:\n"
        f"{inputs}\n"
    )


def save_role_output(artifacts_dir: Path, stage_name: str, iteration: int, stdout: str) -> Path:
    filename = f"{stage_name}.iter{iteration}.out.md"
    path = artifacts_dir / "logs" / filename
    write_text(path, stdout)
    return path


def role_log_path(artifacts_dir: Path, stage_name: str, iteration: int) -> Path:
    return artifacts_dir / "logs" / f"{stage_name}.iter{iteration}.out.md"


def latest_iteration(artifacts_dir: Path, stage_name: str) -> int:
    pattern = re.compile(rf"^{re.escape(stage_name)}\.iter(\d+)\.out\.md$")
    latest = 0
    logs_dir = artifacts_dir / "logs"
    if not logs_dir.exists():
        return 0
    for path in logs_dir.iterdir():
        match = pattern.match(path.name)
        if match:
            latest = max(latest, int(match.group(1)))
    return latest


def parse_role_log_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return extract_json_block(read_text(path)) or {}


def required_evidence_map(task_contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in task_contract.get("required_live_evidence", []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            continue
        result[item_id] = item
    return result


def validate_review_against_contract(review_json: dict[str, Any], task_contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision = str(review_json.get("review_decision", "")).strip()
    if decision != "approved":
        return errors

    required_evidence = required_evidence_map(task_contract)
    completion_policy = task_contract.get("completion_policy", {}) if isinstance(task_contract.get("completion_policy"), dict) else {}

    mandatory_constraints = task_contract.get("non_negotiable_constraints", [])
    if (
        completion_policy.get("require_mandatory_constraints_reported", True)
        and isinstance(mandatory_constraints, list)
        and mandatory_constraints
        and review_json.get("mandatory_constraints_satisfied") is not True
    ):
        errors.append("approved review did not confirm mandatory_constraints_satisfied=true")

    forbidden_detected = review_json.get("forbidden_substitutions_detected", [])
    if (
        completion_policy.get("require_forbidden_substitutions_absent", True)
        and isinstance(forbidden_detected, list)
        and forbidden_detected
    ):
        errors.append("approved review reported forbidden_substitutions_detected")

    review_evidence = review_json.get("required_live_evidence", [])
    if isinstance(review_evidence, list):
        for item in review_evidence:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            status = str(item.get("status", "")).strip().lower()
            if required_evidence.get(item_id, {}).get("required", False) and status != "passed":
                errors.append(f"approved review left required live evidence '{item_id}' in status '{status or 'missing'}'")

    approval_blockers = review_json.get("approval_blockers", [])
    if isinstance(approval_blockers, list) and approval_blockers:
        errors.append("approved review still contains approval_blockers")

    return errors


def review_counts_as_approved(review_json: dict[str, Any], task_contract: dict[str, Any]) -> bool:
    if review_json.get("review_decision") != "approved":
        return False
    return not validate_review_against_contract(review_json, task_contract)


def aggregate_required_evidence_status(
    pipeline_tasks: list[Path],
    artifacts_dir: Path,
    task_contract: dict[str, Any],
) -> dict[str, str]:
    required = required_evidence_map(task_contract)
    statuses = {item_id: "missing" for item_id, item in required.items() if item.get("required", True)}
    for pipeline_task_file in pipeline_tasks:
        task_stub = pipeline_task_file.stem
        latest_review_iteration = latest_iteration(artifacts_dir, f"{task_stub}.code_review")
        if not latest_review_iteration:
            continue
        latest_review_log = role_log_path(artifacts_dir, f"{task_stub}.code_review", latest_review_iteration)
        review_json = parse_role_log_json(latest_review_log)
        review_evidence = review_json.get("required_live_evidence", [])
        if not isinstance(review_evidence, list):
            continue
        for item in review_evidence:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            status = str(item.get("status", "")).strip().lower()
            if item_id not in statuses or not status:
                continue
            current = statuses[item_id]
            if status == "passed":
                statuses[item_id] = "passed"
            elif current == "missing":
                statuses[item_id] = status
    return statuses


def extract_markdown_open_questions(path: Path) -> list[str]:
    """Return explicit questions listed under a markdown open-questions heading."""

    if not path.exists():
        return []

    questions: list[str] = []
    in_section = False
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            if heading in {"open questions", "blocking questions", "открытые вопросы", "блокирующие вопросы"}:
                in_section = True
                continue
            if in_section:
                break
        if not in_section:
            continue
        if re.match(r"^(\d+\.|-|\*)\s+", line):
            questions.append(re.sub(r"^(\d+\.|-|\*)\s+", "", line).strip())
    return [question for question in questions if question]


def infer_stage_statuses_from_artifacts(artifacts_dir: Path) -> dict[str, str]:
    inferred = {
        "Analysis": "pending",
        "Architecture": "pending",
        "Planning": "pending",
        "Development": "pending",
    }

    analysis_review_iter = latest_iteration(artifacts_dir, "analysis_review")
    if analysis_review_iter:
        review_json = parse_role_log_json(role_log_path(artifacts_dir, "analysis_review", analysis_review_iter))
        if not review_json.get("has_critical_issues"):
            inferred["Analysis"] = "approved"

    architecture_review_iter = latest_iteration(artifacts_dir, "architecture_review")
    if architecture_review_iter:
        review_json = parse_role_log_json(role_log_path(artifacts_dir, "architecture_review", architecture_review_iter))
        if not review_json.get("has_critical_issues"):
            inferred["Architecture"] = "approved"

    plan_review_file = artifacts_dir / "plan_review.md"
    if plan_review_file.exists():
        decision = detect_plan_review_status(plan_review_file)
        if decision == "approved":
            inferred["Planning"] = "approved"
        elif decision in {"rejected", "rework_required"}:
            inferred["Planning"] = "blocked"

    if list_pipeline_tasks(artifacts_dir):
        inferred["Development"] = "blocked"

    return inferred


def run_role(
    role_name: str,
    role_prompt_file: Path,
    task_dir: Path,
    artifacts_dir: Path,
    task_file: Path,
    extra_inputs: list[str],
    sandbox_mode: str,
    pipeline_runner: str,
    model_override: str | None = None,
) -> RoleResult:
    prompt = build_role_prompt(
        read_text(role_prompt_file),
        role_name=role_name,
        task_dir=task_dir,
        artifacts_dir=artifacts_dir,
        task_file=task_file,
        extra_inputs=extra_inputs,
        runner=pipeline_runner,
    )
    role_model = model_override or role_models_for(pipeline_runner).get(role_name)
    stdout = run_child_agent(
        pipeline_runner,
        prompt,
        role_model,
        sandbox_mode,
        role_workdir(role_name, sandbox_mode),
    )
    return RoleResult(stdout=stdout, parsed_json=extract_json_block(stdout))


def stage_note(path: Path) -> str:
    rel = path.relative_to(repo_root())
    return f"Updated `{rel}`"


def detect_plan_review_status(review_file: Path) -> str:
    content = read_text(review_file)
    if "ПЛАН УТВЕРЖДЁН" in content:
        return "approved"
    if "ПЛАН ОТКЛОНЁН" in content:
        return "rejected"
    return "rework_required"


def ensure_pipeline_task_dir(artifacts_dir: Path) -> Path:
    task_files_dir = artifacts_dir / "tasks"
    task_files_dir.mkdir(parents=True, exist_ok=True)
    return task_files_dir


def list_pipeline_tasks(artifacts_dir: Path) -> list[Path]:
    return sorted((artifacts_dir / "tasks").glob("task_*_*.md"))


def pipeline_task_label(task_file: Path) -> str:
    match = re.match(r"task_(\d+)_(\d+)$", task_file.stem)
    if match:
        return f"task {match.group(1)}_{match.group(2)}"
    return task_file.stem.replace("_", " ")


def notify_human_in_loop(
    task_dir: Path,
    summary: str,
    requested_action: str,
    artifact_paths: list[Path] | None = None,
) -> None:
    sent, detail = try_send_pipeline_stop_message(
        task_dir=task_dir,
        summary=summary,
        requested_action=requested_action,
        artifact_paths=artifact_paths,
    )
    if sent:
        append_trace(task_dir, "Sent notification about stopped multi-agent pipeline.")
    else:
        append_trace(task_dir, f"Skipped pipeline notification: {detail}")


def notify_pipeline_status(
    task_dir: Path,
    status: str,
    artifact_paths: list[Path] | None = None,
) -> None:
    sent, detail = try_send_pipeline_status_message(
        task_dir=task_dir,
        status=status,
        artifact_paths=artifact_paths,
    )
    if sent:
        append_trace(task_dir, f"Sent pipeline status notification: {status}")
    else:
        append_trace(task_dir, f"Skipped pipeline status notification: {detail}")


def pipeline_runner_label(runner: str) -> str:
    return "Cursor Agent" if runner == "agent" else "Codex"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the multi-agent development pipeline for a task directory via Codex or Cursor Agent CLI.",
    )
    parser.add_argument("task_dir", help="Task directory path.")
    parser.add_argument(
        "--runner",
        choices=PIPELINE_RUNNERS,
        default="agent",
        help="CLI used for each pipeline role (default: agent = Cursor Agent).",
    )
    parser.add_argument(
        "--agents-dir",
        default="/opt/projects/agents",
        help="Directory with the role prompt templates.",
    )
    parser.add_argument(
        "--agents-repo-url",
        default=os.environ.get("CODEX_MULTI_AGENT_PROMPTS_REPO", DEFAULT_AGENTS_REPO_URL),
        help="Git repository to clone when --agents-dir is missing.",
    )
    parser.add_argument(
        "--artifacts-subdir",
        default="multi-agent",
        help="Pipeline artifacts subdirectory inside the task directory.",
    )
    parser.add_argument(
        "--sandbox-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="workspace-write",
        help="Sandbox mode for nested Codex runs.",
    )
    parser.add_argument(
        "--model",
        help="Optional model override for all nested Codex role runs.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the latest unfinished stage recorded in the pipeline artifacts.",
    )
    args = parser.parse_args()
    pipeline_runner = args.runner
    runner_label = pipeline_runner_label(pipeline_runner)

    task_dir = resolve_task_dir(args.task_dir)
    task_file = task_dir / "task.md"
    plan_file = task_dir / "plan.md"
    if not task_file.exists() or not plan_file.exists():
        raise SystemExit(f"Task contract is incomplete in {task_dir}")
    task_contract = load_task_contract(task_dir)
    task_contract_file = task_dir / "task_contract.json"

    agents_dir = ensure_agents_dir(Path(args.agents_dir), args.agents_repo_url, task_dir)
    artifacts_subdir = Path(args.artifacts_subdir)
    if artifacts_subdir.is_absolute() or ".." in artifacts_subdir.parts:
        raise SystemExit(f"Artifacts subdirectory must stay inside the task directory: {args.artifacts_subdir}")
    artifacts_dir = (task_dir / artifacts_subdir).resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    ensure_pipeline_task_dir(artifacts_dir)
    init_pipeline_status(artifacts_dir / "status.md")

    if args.resume:
        append_trace(
            task_dir,
            f"Resumed {runner_label} multi-agent pipeline in `{artifacts_dir.relative_to(repo_root())}`.",
        )
    else:
        append_trace(
            task_dir,
            f"Started {runner_label} multi-agent pipeline in `{artifacts_dir.relative_to(repo_root())}`.",
        )
    write_status(
        task_dir,
        "running",
        f"Running multi-agent development pipeline ({pipeline_runner})",
        {"artifacts_dir": str(artifacts_dir), "pipeline_runner": pipeline_runner},
    )
    if not args.resume:
        notify_pipeline_status(
            task_dir,
            "Started the multi-agent pipeline. Moving to technical specification.",
            [task_file, artifacts_dir / "status.md"],
        )

    notes: list[str] = read_pipeline_notes(artifacts_dir / "status.md") if args.resume else []
    if not notes:
        notes = [stage_note(task_file), stage_note(plan_file)]
        if task_contract_file.exists():
            notes.append(stage_note(task_contract_file))
    elif stage_note(task_file) not in notes:
        notes.extend([stage_note(task_file), stage_note(plan_file)])
        if task_contract_file.exists() and stage_note(task_contract_file) not in notes:
            notes.append(stage_note(task_contract_file))
    stage_statuses = read_pipeline_stage_status(artifacts_dir / "status.md") if args.resume else {}
    inferred_stage_statuses = infer_stage_statuses_from_artifacts(artifacts_dir) if args.resume else {}

    def resolved_stage_status(name: str) -> str:
        file_status = stage_statuses.get(name, "pending")
        inferred_status = inferred_stage_statuses.get(name, "pending")
        if file_status == "approved" or inferred_status == "approved":
            return "approved"
        if file_status == "blocked" or inferred_status == "blocked":
            return "blocked"
        return "pending"

    analysis_status = resolved_stage_status("Analysis")
    architecture_status = resolved_stage_status("Architecture")
    planning_status = resolved_stage_status("Planning")
    development_status = resolved_stage_status("Development")

    def update_status_file() -> None:
        update_pipeline_status(
            artifacts_dir / "status.md",
            analysis=analysis_status,
            architecture=architecture_status,
            planning=planning_status,
            development=development_status,
            notes=notes,
        )

    try:
        analyst_prompt = agents_dir / "02_analyst_prompt.md"
        reviewer_prompt = agents_dir / "03_tz_reviewer_prompt.md"
        previous_analysis_review_log: Path | None = None
        if analysis_status != "approved":
            latest_analysis = latest_iteration(artifacts_dir, "analysis")
            latest_analysis_review = latest_iteration(artifacts_dir, "analysis_review")
            if latest_analysis_review:
                previous_analysis_review_log = role_log_path(artifacts_dir, "analysis_review", latest_analysis_review)

            if latest_analysis > latest_analysis_review:
                iteration = latest_analysis
                write_status(task_dir, "running", f"Analysis review iteration {iteration}")
                review = run_role(
                    "tz_reviewer",
                    reviewer_prompt,
                    task_dir,
                    artifacts_dir,
                    task_file,
                    extra_inputs=[
                        f"Review `{artifacts_dir.relative_to(repo_root())}/technical_specification.md` against the task request.",
                        f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                    ],
                    sandbox_mode=args.sandbox_mode,
                    pipeline_runner=pipeline_runner,
                    model_override=args.model,
                )
                review_log = save_role_output(artifacts_dir, "analysis_review", iteration, review.stdout)
                notes.append(note(stage_note(review_log)))
                previous_analysis_review_log = review_log
                review_json = review.parsed_json or {}
                if not review_json.get("has_critical_issues"):
                    analysis_status = "approved"
                    notify_pipeline_status(
                        task_dir,
                        "Finished technical specification review. Moving to architecture.",
                        [
                            artifacts_dir / "technical_specification.md",
                            review_log,
                        ],
                    )
                elif iteration == 2:
                    analysis_status = "blocked"
                    notes.append(note("Analysis review still has critical issues after 2 iterations"))
                    update_status_file()
                    write_status(task_dir, "blocked", "Analysis stage failed review in multi-agent pipeline")
                    append_trace(task_dir, "Multi-agent pipeline stopped after analysis review did not converge.")
                    notify_human_in_loop(
                        task_dir,
                        "Analysis review still has critical issues after 2 iterations.",
                        "Inspect the latest technical specification and review artifacts, clarify the task handoff, then resume the pipeline.",
                        [
                            artifacts_dir / "technical_specification.md",
                            review_log,
                            task_file,
                        ],
                    )
                    return
            else:
                start_iteration = latest_analysis_review + 1 if latest_analysis_review else 1
                for iteration in range(start_iteration, 3):
                    write_status(task_dir, "running", f"Analysis iteration {iteration}")
                    analyst_inputs = [
                        f"Read the user task from `{task_file.relative_to(repo_root())}`.",
                        f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                        "Produce a technical specification for implementing this repository task.",
                    ]
                    if previous_analysis_review_log is not None:
                        analyst_inputs.append(
                            f"Treat `{previous_analysis_review_log.relative_to(repo_root())}` as the reviewer feedback that must be addressed without unnecessary unrelated changes."
                        )
                    analyst = run_role(
                        "analyst",
                        analyst_prompt,
                        task_dir,
                        artifacts_dir,
                        task_file,
                        extra_inputs=analyst_inputs,
                        sandbox_mode=args.sandbox_mode,
                        pipeline_runner=pipeline_runner,
                        model_override=args.model,
                    )
                    analyst_log = save_role_output(artifacts_dir, "analysis", iteration, analyst.stdout)
                    notes.append(note(stage_note(analyst_log)))
                    analyst_json = analyst.parsed_json or {}
                    blocking = analyst_json.get("blocking_questions") or []
                    if not blocking:
                        blocking = extract_markdown_open_questions(artifacts_dir / "technical_specification.md")
                    if blocking:
                        analysis_status = "blocked"
                        notes.append(note(f"Analysis blocked by questions: {len(blocking)}"))
                        update_status_file()
                        write_status(task_dir, "blocked", "Multi-agent analysis blocked by open questions", {"blocking_questions": blocking})
                        append_trace(task_dir, "Multi-agent pipeline stopped in analysis due to blocking questions.")
                        notify_human_in_loop(
                            task_dir,
                            f"Analysis produced {len(blocking)} blocking questions.",
                            "Open the task artifacts, answer the blocking questions in the handoff, then resume the pipeline.",
                            [
                                artifacts_dir / "technical_specification.md",
                                analyst_log,
                                task_file,
                            ],
                        )
                        return

                    notify_pipeline_status(
                        task_dir,
                        "Finished technical specification draft. Moving to technical specification review.",
                        [
                            artifacts_dir / "technical_specification.md",
                            analyst_log,
                        ],
                    )
                    review = run_role(
                        "tz_reviewer",
                        reviewer_prompt,
                        task_dir,
                        artifacts_dir,
                        task_file,
                        extra_inputs=[
                            f"Review `{artifacts_dir.relative_to(repo_root())}/technical_specification.md` against the task request.",
                            f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                        ],
                        sandbox_mode=args.sandbox_mode,
                        pipeline_runner=pipeline_runner,
                        model_override=args.model,
                    )
                    review_log = save_role_output(artifacts_dir, "analysis_review", iteration, review.stdout)
                    notes.append(note(stage_note(review_log)))
                    previous_analysis_review_log = review_log
                    review_json = review.parsed_json or {}
                    if not review_json.get("has_critical_issues"):
                        analysis_status = "approved"
                        notify_pipeline_status(
                            task_dir,
                            "Finished technical specification review. Moving to architecture.",
                            [
                                artifacts_dir / "technical_specification.md",
                                review_log,
                            ],
                        )
                        break
                    if iteration == 2:
                        analysis_status = "blocked"
                        notes.append(note("Analysis review still has critical issues after 2 iterations"))
                        update_status_file()
                        write_status(task_dir, "blocked", "Analysis stage failed review in multi-agent pipeline")
                        append_trace(task_dir, "Multi-agent pipeline stopped after analysis review did not converge.")
                        notify_human_in_loop(
                            task_dir,
                            "Analysis review still has critical issues after 2 iterations.",
                            "Inspect the latest technical specification and review artifacts, clarify the task handoff, then resume the pipeline.",
                            [
                                artifacts_dir / "technical_specification.md",
                                review_log,
                                task_file,
                            ],
                        )
                        return
            update_status_file()

        architect_prompt = agents_dir / "04_architect_prompt.md"
        architecture_reviewer_prompt = agents_dir / "05_architecture_reviewer_prompt.md"
        previous_architecture_review_log: Path | None = None
        if architecture_status != "approved":
            latest_architecture = latest_iteration(artifacts_dir, "architecture")
            latest_architecture_review = latest_iteration(artifacts_dir, "architecture_review")
            if latest_architecture_review:
                previous_architecture_review_log = role_log_path(artifacts_dir, "architecture_review", latest_architecture_review)

            if latest_architecture > latest_architecture_review:
                iteration = latest_architecture
                write_status(task_dir, "running", f"Architecture review iteration {iteration}")
                architecture_review = run_role(
                    "architecture_reviewer",
                    architecture_reviewer_prompt,
                    task_dir,
                    artifacts_dir,
                    task_file,
                    extra_inputs=[
                        f"Review `{artifacts_dir.relative_to(repo_root())}/architecture.md` against the task and technical specification.",
                        f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                    ],
                    sandbox_mode=args.sandbox_mode,
                    pipeline_runner=pipeline_runner,
                    model_override=args.model,
                )
                review_log = save_role_output(artifacts_dir, "architecture_review", iteration, architecture_review.stdout)
                notes.append(note(stage_note(review_log)))
                previous_architecture_review_log = review_log
                review_json = architecture_review.parsed_json or {}
                if not review_json.get("has_critical_issues"):
                    architecture_status = "approved"
                    notify_pipeline_status(
                        task_dir,
                        "Finished architecture review. Moving to planning.",
                        [
                            artifacts_dir / "architecture.md",
                            review_log,
                        ],
                    )
                elif iteration == 2:
                    architecture_status = "blocked"
                    notes.append(note("Architecture review still has critical issues after 2 iterations"))
                    update_status_file()
                    write_status(task_dir, "blocked", "Architecture stage failed review in multi-agent pipeline")
                    append_trace(task_dir, "Multi-agent pipeline stopped after architecture review did not converge.")
                    notify_human_in_loop(
                        task_dir,
                        "Architecture review still has critical issues after 2 iterations.",
                        "Inspect the architecture artifact and review feedback, adjust the task handoff or architecture direction, then resume the pipeline.",
                        [
                            artifacts_dir / "architecture.md",
                            review_log,
                            task_file,
                        ],
                    )
                    return
            else:
                start_iteration = latest_architecture_review + 1 if latest_architecture_review else 1
                for iteration in range(start_iteration, 3):
                    write_status(task_dir, "running", f"Architecture iteration {iteration}")
                    architect_inputs = [
                        f"Use `{artifacts_dir.relative_to(repo_root())}/technical_specification.md` as the approved technical specification.",
                        f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                    ]
                    if previous_architecture_review_log is not None:
                        architect_inputs.append(
                            f"Treat `{previous_architecture_review_log.relative_to(repo_root())}` as the reviewer feedback that must be addressed without unnecessary unrelated changes."
                        )
                    architecture = run_role(
                        "architect",
                        architect_prompt,
                        task_dir,
                        artifacts_dir,
                        task_file,
                        extra_inputs=architect_inputs,
                        sandbox_mode=args.sandbox_mode,
                        pipeline_runner=pipeline_runner,
                        model_override=args.model,
                    )
                    architecture_log = save_role_output(artifacts_dir, "architecture", iteration, architecture.stdout)
                    notes.append(note(stage_note(architecture_log)))
                    notify_pipeline_status(
                        task_dir,
                        "Finished architecture draft. Moving to architecture review.",
                        [
                            artifacts_dir / "architecture.md",
                            architecture_log,
                        ],
                    )

                    architecture_review = run_role(
                        "architecture_reviewer",
                        architecture_reviewer_prompt,
                        task_dir,
                        artifacts_dir,
                        task_file,
                        extra_inputs=[
                            f"Review `{artifacts_dir.relative_to(repo_root())}/architecture.md` against the task and technical specification.",
                            f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                        ],
                        sandbox_mode=args.sandbox_mode,
                        pipeline_runner=pipeline_runner,
                        model_override=args.model,
                    )
                    review_log = save_role_output(artifacts_dir, "architecture_review", iteration, architecture_review.stdout)
                    notes.append(note(stage_note(review_log)))
                    previous_architecture_review_log = review_log
                    review_json = architecture_review.parsed_json or {}
                    if not review_json.get("has_critical_issues"):
                        architecture_status = "approved"
                        notify_pipeline_status(
                            task_dir,
                            "Finished architecture review. Moving to planning.",
                            [
                                artifacts_dir / "architecture.md",
                                review_log,
                            ],
                        )
                        break
                    if iteration == 2:
                        architecture_status = "blocked"
                        notes.append(note("Architecture review still has critical issues after 2 iterations"))
                        update_status_file()
                        write_status(task_dir, "blocked", "Architecture stage failed review in multi-agent pipeline")
                        append_trace(task_dir, "Multi-agent pipeline stopped after architecture review did not converge.")
                        notify_human_in_loop(
                            task_dir,
                            "Architecture review still has critical issues after 2 iterations.",
                            "Inspect the architecture artifact and review feedback, adjust the task handoff or architecture direction, then resume the pipeline.",
                            [
                                artifacts_dir / "architecture.md",
                                review_log,
                                task_file,
                            ],
                        )
                        return
            update_status_file()

        planner_prompt = agents_dir / "06_agent_planner.md"
        plan_reviewer_prompt = agents_dir / "07_agent_plan_reviewer.md"
        previous_plan_review_log: Path | None = None
        if planning_status != "approved":
            latest_planning = latest_iteration(artifacts_dir, "planning")
            latest_planning_review = latest_iteration(artifacts_dir, "planning_review")
            if latest_planning_review:
                previous_plan_review_log = role_log_path(artifacts_dir, "planning_review", latest_planning_review)

            if latest_planning > latest_planning_review:
                iteration = latest_planning
                write_status(task_dir, "running", f"Planning review iteration {iteration}")
                plan_review = run_role(
                    "plan_reviewer",
                    plan_reviewer_prompt,
                    task_dir,
                    artifacts_dir,
                    task_file,
                    extra_inputs=[
                        f"Review `{artifacts_dir.relative_to(repo_root())}/plan.md` and the generated task description files.",
                        f"Write the review to `{artifacts_dir.relative_to(repo_root())}/plan_review.md`.",
                    ],
                    sandbox_mode=args.sandbox_mode,
                    pipeline_runner=pipeline_runner,
                    model_override=args.model,
                )
                review_log = save_role_output(artifacts_dir, "planning_review", iteration, plan_review.stdout)
                notes.append(note(stage_note(review_log)))
                previous_plan_review_log = review_log
                review_file = artifacts_dir / "plan_review.md"
                decision = detect_plan_review_status(review_file) if review_file.exists() else "rework_required"
                if decision == "approved":
                    planning_status = "approved"
                    notify_pipeline_status(
                        task_dir,
                        "Finished plan review. Moving to implementation tasks.",
                        [
                            artifacts_dir / "plan.md",
                            review_log,
                        ],
                    )
                elif iteration == 2:
                    planning_status = "blocked"
                    notes.append(note("Plan review still requires rework after 2 iterations"))
                    update_status_file()
                    write_status(task_dir, "blocked", "Planning stage failed review in multi-agent pipeline")
                    append_trace(task_dir, "Multi-agent pipeline stopped after plan review did not converge.")
                    notify_human_in_loop(
                        task_dir,
                        "Plan review still requires rework after 2 iterations.",
                        "Inspect the generated plan and review artifact, update the task direction if needed, then resume the pipeline.",
                        [
                            artifacts_dir / "plan.md",
                            review_log,
                            task_file,
                        ],
                    )
                    return
            else:
                start_iteration = latest_planning_review + 1 if latest_planning_review else 1
                for iteration in range(start_iteration, 3):
                    write_status(task_dir, "running", f"Planning iteration {iteration}")
                    planner_inputs = [
                        f"Use `{artifacts_dir.relative_to(repo_root())}/technical_specification.md` and `{artifacts_dir.relative_to(repo_root())}/architecture.md` as approved inputs.",
                        f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                        "Create development tasks under the pipeline artifacts directory.",
                    ]
                    if previous_plan_review_log is not None:
                        planner_inputs.append(
                            f"Treat `{previous_plan_review_log.relative_to(repo_root())}` as the previous plan review feedback that must be addressed."
                        )
                    planner = run_role(
                        "planner",
                        planner_prompt,
                        task_dir,
                        artifacts_dir,
                        task_file,
                        extra_inputs=planner_inputs,
                        sandbox_mode=args.sandbox_mode,
                        pipeline_runner=pipeline_runner,
                        model_override=args.model,
                    )
                    planner_log = save_role_output(artifacts_dir, "planning", iteration, planner.stdout)
                    notes.append(note(stage_note(planner_log)))
                    notify_pipeline_status(
                        task_dir,
                        "Finished implementation planning draft. Moving to plan review.",
                        [
                            artifacts_dir / "plan.md",
                            planner_log,
                        ],
                    )

                    plan_review = run_role(
                        "plan_reviewer",
                        plan_reviewer_prompt,
                        task_dir,
                        artifacts_dir,
                        task_file,
                        extra_inputs=[
                            f"Review `{artifacts_dir.relative_to(repo_root())}/plan.md` and the generated task description files.",
                            f"Write the review to `{artifacts_dir.relative_to(repo_root())}/plan_review.md`.",
                        ],
                        sandbox_mode=args.sandbox_mode,
                        pipeline_runner=pipeline_runner,
                        model_override=args.model,
                    )
                    review_log = save_role_output(artifacts_dir, "planning_review", iteration, plan_review.stdout)
                    notes.append(note(stage_note(review_log)))
                    previous_plan_review_log = review_log
                    review_file = artifacts_dir / "plan_review.md"
                    decision = detect_plan_review_status(review_file) if review_file.exists() else "rework_required"
                    if decision == "approved":
                        planning_status = "approved"
                        notify_pipeline_status(
                            task_dir,
                            "Finished plan review. Moving to implementation tasks.",
                            [
                                artifacts_dir / "plan.md",
                                review_log,
                            ],
                        )
                        break
                    if iteration == 2:
                        planning_status = "blocked"
                        notes.append(note("Plan review still requires rework after 2 iterations"))
                        update_status_file()
                        write_status(task_dir, "blocked", "Planning stage failed review in multi-agent pipeline")
                        append_trace(task_dir, "Multi-agent pipeline stopped after plan review did not converge.")
                        notify_human_in_loop(
                            task_dir,
                            "Plan review still requires rework after 2 iterations.",
                            "Inspect the generated plan and review artifact, update the task direction if needed, then resume the pipeline.",
                            [
                                artifacts_dir / "plan.md",
                                review_log,
                                task_file,
                            ],
                        )
                        return
            update_status_file()

        developer_prompt = agents_dir / "08_agent_developer.md"
        code_reviewer_prompt = agents_dir / "09_agent_code_reviewer.md"
        pipeline_tasks = list_pipeline_tasks(artifacts_dir)
        if not pipeline_tasks:
            development_status = "blocked"
            notes.append(note("Planner did not produce any pipeline task files"))
            update_status_file()
            write_status(task_dir, "blocked", "Planner did not create implementation task files")
            append_trace(task_dir, "Multi-agent pipeline stopped because no implementation tasks were produced.")
            notify_human_in_loop(
                task_dir,
                "Planning completed without any implementation task files.",
                "Inspect the planning artifacts, fix the planning stage outputs, then resume the pipeline.",
                [
                    artifacts_dir / "plan.md",
                    artifacts_dir / "plan_review.md",
                    task_file,
                ],
            )
            return

        for task_index, pipeline_task_file in enumerate(pipeline_tasks, start=1):
            task_stub = pipeline_task_file.stem
            task_label = pipeline_task_label(pipeline_task_file)
            latest_dev_iteration = latest_iteration(artifacts_dir, f"{task_stub}.developer")
            latest_review_iteration = latest_iteration(artifacts_dir, f"{task_stub}.code_review")
            latest_review_log = role_log_path(artifacts_dir, f"{task_stub}.code_review", latest_review_iteration) if latest_review_iteration else None
            latest_review_json = parse_role_log_json(latest_review_log) if latest_review_log else {}
            if review_counts_as_approved(latest_review_json, task_contract):
                continue

            previous_code_review_log = latest_review_log
            if latest_dev_iteration > latest_review_iteration:
                start_iteration = latest_dev_iteration
                pending_review_only = True
            else:
                start_iteration = latest_review_iteration + 1 if latest_review_iteration else 1
                pending_review_only = False

            for iteration in range(start_iteration, 3):
                if not pending_review_only:
                    write_status(task_dir, "running", f"Development task {task_index}/{len(pipeline_tasks)} iteration {iteration}")
                    developer_inputs = [
                        f"Implement the repository changes described in `{pipeline_task_file.relative_to(repo_root())}`.",
                        f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                        f"Use the original repository task file `{task_file.relative_to(repo_root())}` for global context.",
                    ]
                    if previous_code_review_log is not None:
                        developer_inputs.append(
                            f"Treat `{previous_code_review_log.relative_to(repo_root())}` as the previous code review feedback and address only the noted issues."
                        )
                    developer = run_role(
                        "developer",
                        developer_prompt,
                        task_dir,
                        artifacts_dir,
                        task_file,
                        extra_inputs=developer_inputs,
                        sandbox_mode=args.sandbox_mode,
                        pipeline_runner=pipeline_runner,
                        model_override=args.model,
                    )
                    developer_log = save_role_output(artifacts_dir, f"{task_stub}.developer", iteration, developer.stdout)
                    notes.append(note(stage_note(developer_log)))
                    developer_json = developer.parsed_json or {}
                    if developer_json.get("stage_status") == "has_open_questions":
                        development_status = "blocked"
                        notes.append(note(f"Developer raised open questions for `{pipeline_task_file.name}`"))
                        update_status_file()
                        write_status(task_dir, "blocked", "Developer raised open questions in multi-agent pipeline")
                        append_trace(task_dir, f"Multi-agent pipeline stopped with open questions in `{pipeline_task_file.name}`.")
                        notify_human_in_loop(
                            task_dir,
                            f"Developer raised open questions for {pipeline_task_file.name}.",
                            "Inspect the latest developer artifact and task file, answer the open questions in the handoff, then resume the pipeline.",
                            [
                                pipeline_task_file,
                                developer_log,
                                task_file,
                            ],
                        )
                        return
                    notify_pipeline_status(
                        task_dir,
                        f"Finished {task_label}. Moving to code review.",
                        [
                            pipeline_task_file,
                            developer_log,
                        ],
                    )
                else:
                    write_status(task_dir, "running", f"Development review task {task_index}/{len(pipeline_tasks)} iteration {iteration}")
                    pending_review_only = False

                reviewer = run_role(
                    "code_reviewer",
                    code_reviewer_prompt,
                    task_dir,
                    artifacts_dir,
                    task_file,
                    extra_inputs=[
                        f"Review the implementation for `{pipeline_task_file.relative_to(repo_root())}`.",
                        f"Use `{artifacts_dir.relative_to(repo_root())}` as `artifacts_dir`.",
                        f"Use `{pipeline_task_file.relative_to(repo_root())}` as the task spec being reviewed.",
                    ],
                    sandbox_mode=args.sandbox_mode,
                    pipeline_runner=pipeline_runner,
                    model_override=args.model,
                )
                reviewer_log = save_role_output(artifacts_dir, f"{task_stub}.code_review", iteration, reviewer.stdout)
                notes.append(note(stage_note(reviewer_log)))
                previous_code_review_log = reviewer_log
                reviewer_json = reviewer.parsed_json or {}
                decision = reviewer_json.get("review_decision")
                review_contract_errors = validate_review_against_contract(reviewer_json, task_contract)
                if review_contract_errors:
                    development_status = "blocked"
                    notes.append(note(f"Review for `{pipeline_task_file.name}` violated task contract: {'; '.join(review_contract_errors)}"))
                    update_status_file()
                    write_status(task_dir, "blocked", f"Review approval violated task contract for {pipeline_task_file.name}")
                    append_trace(
                        task_dir,
                        f"Multi-agent pipeline stopped because review approval for `{pipeline_task_file.name}` violated the task contract: {'; '.join(review_contract_errors)}."
                    )
                    notify_human_in_loop(
                        task_dir,
                        f"Review approval for {pipeline_task_file.name} violated the task contract.",
                        "Inspect the latest code review output and task contract, fix the prompt or implementation gap, then resume the pipeline.",
                        [
                            pipeline_task_file,
                            reviewer_log,
                            task_contract_file,
                            task_file,
                        ],
                    )
                    return
                if decision == "approved":
                    next_task = pipeline_tasks[task_index] if task_index < len(pipeline_tasks) else None
                    if next_task is not None:
                        notify_pipeline_status(
                            task_dir,
                            f"Finished code review for {task_label}. Moving to {pipeline_task_label(next_task)}.",
                            [
                                reviewer_log,
                                next_task,
                            ],
                        )
                    else:
                        notify_pipeline_status(
                            task_dir,
                            f"Finished code review for {task_label}. Pipeline implementation is complete.",
                            [
                                reviewer_log,
                                task_dir / "status.json",
                            ],
                        )
                    break
                if decision == "blocked":
                    development_status = "blocked"
                    notes.append(note(f"Code review blocked `{pipeline_task_file.name}`"))
                    update_status_file()
                    write_status(task_dir, "blocked", f"Code review blocked progress for {pipeline_task_file.name}")
                    append_trace(task_dir, f"Multi-agent pipeline stopped because code review marked `{pipeline_task_file.name}` as blocked.")
                    notify_human_in_loop(
                        task_dir,
                        f"Code review blocked progress for {pipeline_task_file.name}.",
                        "Inspect the implementation task, latest code review feedback, and task contract, then resume after resolving the blocker.",
                        [
                            pipeline_task_file,
                            reviewer_log,
                            task_contract_file,
                            task_file,
                        ],
                    )
                    return
                if iteration == 2:
                    development_status = "blocked"
                    notes.append(note(f"Code review rejected `{pipeline_task_file.name}` after 2 iterations"))
                    update_status_file()
                    write_status(task_dir, "blocked", f"Code review did not converge for {pipeline_task_file.name}")
                    append_trace(task_dir, f"Multi-agent pipeline stopped after code review did not converge for `{pipeline_task_file.name}`.")
                    notify_human_in_loop(
                        task_dir,
                        f"Code review did not converge for {pipeline_task_file.name} after 2 iterations.",
                        "Inspect the implementation task and latest code review feedback, decide what to change, then resume the pipeline.",
                        [
                            pipeline_task_file,
                            reviewer_log,
                            task_file,
                        ],
                    )
                    return

        evidence_statuses = aggregate_required_evidence_status(pipeline_tasks, artifacts_dir, task_contract)
        missing_required_evidence = [item_id for item_id, status in evidence_statuses.items() if status != "passed"]
        if missing_required_evidence:
            development_status = "blocked"
            notes.append(
                note(
                    "Required live evidence not passed: "
                    + ", ".join(f"{item_id}={evidence_statuses[item_id]}" for item_id in missing_required_evidence)
                )
            )
            update_status_file()
            write_status(task_dir, "blocked", "Required live evidence did not pass before pipeline completion")
            append_trace(
                task_dir,
                "Multi-agent pipeline stopped before completion because required live evidence did not pass: "
                + ", ".join(f"{item_id}={evidence_statuses[item_id]}" for item_id in missing_required_evidence)
                + "."
            )
            notify_human_in_loop(
                task_dir,
                "Required live evidence did not pass before pipeline completion.",
                "Inspect the latest implementation reviews and required evidence status, then resume after the missing runtime verification is actually passed.",
                [
                    task_contract_file,
                    task_dir / "status.json",
                ],
            )
            return

        development_status = "approved"
        update_status_file()
        write_status(task_dir, "completed", "Multi-agent development pipeline completed", {"artifacts_dir": str(artifacts_dir)})
        append_trace(
            task_dir,
            f"Completed {runner_label} multi-agent pipeline in `{artifacts_dir.relative_to(repo_root())}`.",
        )
    except Exception as exc:
        notes.append(note(f"Pipeline failed: {exc}"))
        if analysis_status == "pending":
            analysis_status = "blocked"
        update_status_file()
        write_status(task_dir, "blocked", f"Multi-agent pipeline failed: {exc}")
        append_trace(task_dir, f"Multi-agent pipeline failed: {exc}")
        notify_human_in_loop(
            task_dir,
            f"Multi-agent pipeline failed with an exception: {exc}",
            "Inspect the task trace and runner log in CLI, fix the blocker, then rerun or resume the pipeline.",
            [
                task_dir / "trace.md",
                task_dir / ".runner" / "runner.log",
                task_file,
            ],
        )
        raise


if __name__ == "__main__":
    main()
