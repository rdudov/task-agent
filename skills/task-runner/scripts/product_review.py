#!/usr/bin/env python3
"""Digest-bound product-review records shared by launch and completion owners."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

try:  # package install
    from . import review_admission, write_admission
except ImportError:  # direct repository script
    import review_admission
    import write_admission


SCHEMA_VERSION = 1
STAGES = frozenset({"statement", "completion"})
PASSING_VERDICT = "satisfied"
VERDICTS = frozenset({PASSING_VERDICT, "not_satisfied", "not_established"})
VERBATIM_USER_WORDS = "user-verbatim.json"
COMPARISON_OUTCOMES = frozenset(
    {"satisfied", "not_a_requirement", "out_of_scope", "not_satisfied"}
)


class _VisibleTextParser(HTMLParser):
    """Collect user-visible HTML text while ignoring non-rendered containers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_tags: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"head", "script", "style", "template"}:
            self.hidden_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.hidden_tags:
            self.hidden_tags.remove(tag)

    def handle_data(self, data: str) -> None:
        if not self.hidden_tags:
            self.parts.append(data)


def statement_text_from_report(path: Path) -> str:
    parser = _VisibleTextParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return " ".join(parser.parts)


def _statement_body(markdown: str) -> str:
    """Remove task metadata that is not part of the readable statement body."""
    if markdown.startswith("---\n"):
        _opening, separator, remainder = markdown.partition("\n---\n")
        if separator:
            return remainder
    return markdown


def _authored_statement(markdown: str) -> str:
    """Exclude lifecycle-owned metadata and the chronological Status journal."""
    body = _statement_body(markdown)
    return re.sub(
        r"(?ms)^## Status[^\S\n]*\n.*?\n(?=^## )|(?:^|(?<=\n)\n)## Status[^\S\n]*\n(?!.*^## ).*\Z",
        "",
        body,
    )


def _readable_tokens(text: str, *, markdown: bool = False) -> list[str]:
    if markdown:
        text = _statement_body(text)
        text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return [token.casefold() for token in re.findall(r"[\w]+", text, flags=re.UNICODE)]


def report_contains_readable_statement(report: Path, statement: str) -> bool:
    """Whether the complete normalized statement appears once as visible text."""
    expected = _readable_tokens(_authored_statement(statement), markdown=True)
    observed = _readable_tokens(statement_text_from_report(report))
    if not expected or len(observed) < len(expected):
        return False
    expected_index = 0
    for token in observed:
        if token == expected[expected_index]:
            expected_index += 1
            if expected_index == len(expected):
                return True
    return False


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def statement_sha256(path: Path) -> str:
    """Hash authored statement meaning without lifecycle-owned task state."""
    try:
        body = _authored_statement(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"task statement is missing or unreadable: {path}") from exc
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def canonical_json_sha256(path: Path) -> str:
    """Hash JSON meaning rather than formatting rewritten by lifecycle owners."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON input is missing or unreadable: {path}") from exc
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def result_path(task_dir: Path, stage: str) -> Path:
    if stage not in STAGES:
        raise ValueError(f"unsupported product-review stage: {stage}")
    return task_dir / "product-review" / f"{stage}.json"


def report_path(task_dir: Path, stage: str) -> Path:
    if stage not in STAGES:
        raise ValueError(f"unsupported product-review stage: {stage}")
    if stage == "completion":
        return task_dir / "deliverables" / "product-review.html"
    return task_dir / "deliverables" / "statement-review.html"


def verbatim_path(task_dir: Path) -> Path:
    return task_dir / VERBATIM_USER_WORDS


def task_relative_path(task_dir: Path, path: Path) -> str:
    """Return one normalized task-relative path without narrowing it to the root."""
    try:
        relative = path.resolve().relative_to(task_dir.resolve())
    except ValueError as exc:
        raise ValueError("product-review packet must be inside the task directory") from exc
    if not relative.parts or relative == Path("."):
        raise ValueError("product-review packet must name a file inside the task directory")
    return relative.as_posix()


def load_verbatim_record(task_dir: Path) -> dict:
    path = verbatim_path(task_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"verbatim user words are missing or unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("verbatim user words must use schema_version 1")
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("verbatim user words contain no messages")
    excluded = value.get("excluded_messages", [])
    if not isinstance(excluded, list):
        raise ValueError("excluded verbatim messages must be an array")
    identities: set[tuple[str, str]] = set()
    for message in [*messages, *excluded]:
        if not isinstance(message, dict):
            raise ValueError("each verbatim user message must be an object")
        for name in ("channel", "source_id", "occurred_at", "text"):
            if not isinstance(message.get(name), str) or not message[name].strip():
                raise ValueError(f"verbatim user message has no {name}")
        identity = (message["channel"], message["source_id"])
        if identity in identities:
            raise ValueError("verbatim user words contain a duplicate message identity")
        identities.add(identity)
    for message in excluded:
        if not isinstance(message.get("reason"), str) or not message["reason"].strip():
            raise ValueError("an excluded verbatim message must name the reason")
    return value


def load_verbatim_messages(task_dir: Path) -> list[dict]:
    return load_verbatim_record(task_dir)["messages"]


def comparison_instruction(stage: str) -> str:
    """Return the one reviewer-output contract enforced by this module."""
    if stage not in STAGES:
        raise ValueError(f"unsupported product-review stage: {stage}")
    subject = "statement" if stage == "statement" else "observed result"
    return (
        "Write `requirement_comparison` entries with `source_ids`, `requirement`, "
        "`observed_result`, and `outcome`. Cover every source_id from both "
        "`messages` and `excluded_messages`, and map every substantive requirement "
        f"separately to the exact {subject}. Use `satisfied` only when it is met, "
        "`not_satisfied` when it is not, `not_a_requirement` with `reason` for an "
        "included receipt/question that adds no requirement, and `out_of_scope` "
        "with `reason` only for a source already present in `excluded_messages`. "
        "Challenge every exclusion; a hidden requirement is `not_satisfied`, not "
        "`out_of_scope`. A satisfied verdict cannot contain `not_satisfied`."
    )


def _feedback_records(value: object) -> list[dict]:
    if not isinstance(value, dict):
        return []
    records = value.get("records")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return [value] if value.get("classification") else []


def is_plain_approval(text: str) -> bool:
    """Accept only a bounded acknowledgement with no additional proposition."""
    normalized = re.sub(r"\s+", " ", text.strip().casefold())
    return bool(re.fullmatch(
        r"(?:да[,. ]+)?(?:получил(?:а)?|согласен|согласна|одобряю|подтверждаю|"
        r"разрешаю продолжить|можно продолжать|продолжай(?:те)?|ок(?:ей)?|спасибо)"
        r"(?:[,.! ]+(?:спасибо|вс[её] верно|можно продолжать|продолжай(?:те)?))?[.! ]*",
        normalized,
    ))


def _approval_sources(task_dir: Path, stage: str, reviewed: object) -> set[str]:
    """Return authenticated acknowledgements that do not stale a stage verdict."""
    feedback_path = task_dir / "product-review" / f"{stage}-feedback.json"
    try:
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    try:
        verbatim = load_verbatim_record(task_dir)
    except ValueError:
        return set()
    messages = {
        message["source_id"]: message
        for message in verbatim["messages"]
        if message.get("channel") == "gmail"
    }
    sources: set[str] = set()
    expected_digest = reviewed
    for record in _feedback_records(feedback):
        target = record.get("preserves_review") or record.get("target_review")
        if not isinstance(target, dict) or target.get(
            "verbatim_user_words_sha256"
        ) != reviewed:
            continue
        source_id = record.get("gmail_id")
        message = messages.get(source_id) if isinstance(source_id, str) else None
        exact_text = message.get("text") if isinstance(message, dict) else None
        if (
            record.get("classification") == "approval"
            and record.get("approval_verified") is True
            and isinstance(exact_text, str)
            and is_plain_approval(exact_text)
            and record.get("verbatim_text_sha256")
            == hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
            and record.get("verbatim_before_append_sha256") == expected_digest
            and isinstance(record.get("verbatim_after_append_sha256"), str)
            and source_id not in sources
        ):
            sources.add(source_id)
            expected_digest = record["verbatim_after_append_sha256"]
        else:
            return set()
    if not sources or expected_digest != verbatim_sha256(task_dir):
        return set()
    return sources


def verbatim_record_for_result(task_dir: Path, stage: str, value: dict) -> dict:
    """Return the exact user-message set a current result was required to cover."""
    verbatim = load_verbatim_record(task_dir)
    reviewed = value.get("verbatim_user_words_sha256")
    if reviewed == verbatim_sha256(task_dir):
        return verbatim
    approval_sources = _approval_sources(task_dir, stage, reviewed)
    if not approval_sources:
        raise ValueError(f"verbatim user words changed after {stage} review")
    filtered = dict(verbatim)
    filtered["messages"] = [
        message
        for message in verbatim["messages"]
        if message["source_id"] not in approval_sources
    ]
    return filtered


def validate_requirement_comparison(
    verbatim: dict, value: dict, stage: str, *, require_passing: bool = True
) -> list[dict]:
    """Validate the comparison shared by the reviewer prompt, gate, and mail."""
    comparison = value.get("requirement_comparison")
    if not isinstance(comparison, list) or not comparison:
        raise ValueError("product-review result has no per-requirement verbatim comparison")
    included_sources = {message["source_id"] for message in verbatim["messages"]}
    excluded_sources = {
        message["source_id"] for message in verbatim.get("excluded_messages", [])
    }
    excluded_reasons = {
        message["source_id"]: message["reason"]
        for message in verbatim.get("excluded_messages", [])
    }
    expected_sources = included_sources | excluded_sources
    covered: set[str] = set()
    outcomes: set[str] = set()
    for item in comparison:
        if not isinstance(item, dict):
            raise ValueError("verbatim requirement comparison is malformed")
        source_ids = item.get("source_ids")
        if not isinstance(source_ids, list) or not source_ids or not all(
            isinstance(source_id, str) and source_id for source_id in source_ids
        ):
            raise ValueError("verbatim requirement comparison has no source identities")
        row_sources = set(source_ids)
        if not row_sources <= expected_sources:
            raise ValueError("verbatim requirement comparison names an unknown user message")
        covered.update(row_sources)
        if not isinstance(item.get("requirement"), str) or not item["requirement"].strip():
            raise ValueError("verbatim requirement comparison has no requirement text")
        if not isinstance(item.get("observed_result"), str) or not item["observed_result"].strip():
            raise ValueError("verbatim requirement comparison has no observed result")
        outcome = item.get("outcome")
        if outcome not in COMPARISON_OUTCOMES:
            raise ValueError("verbatim requirement comparison has an invalid outcome")
        outcomes.add(outcome)
        reason = str(item.get("reason") or "").strip()
        if outcome in {"not_a_requirement", "out_of_scope"} and not reason:
            raise ValueError(f"a {outcome} user message must name the reason")
        if require_passing and outcome == "not_satisfied":
            raise ValueError(
                "satisfied verdict conflicts with the verbatim requirement comparison"
            )
        if row_sources & excluded_sources and (
            outcome != "out_of_scope" or not row_sources <= excluded_sources
        ):
            raise ValueError(
                "an excluded user message must be reviewed explicitly as out_of_scope"
            )
        if outcome == "out_of_scope" and not row_sources <= excluded_sources:
            raise ValueError("only excluded_messages may be reviewed as out_of_scope")
        if outcome == "out_of_scope":
            if len(row_sources) != 1:
                raise ValueError("each out_of_scope message must have its own comparison row")
            source_id = next(iter(row_sources))
            if reason != excluded_reasons[source_id]:
                raise ValueError(
                    "an out_of_scope comparison must preserve the recorded exclusion reason"
                )
    if covered != expected_sources:
        raise ValueError("verbatim requirement comparison does not cover every user message")
    if value.get("verdict") == "not_satisfied" and "not_satisfied" not in outcomes:
        raise ValueError("not_satisfied verdict has no unmet verbatim requirement")
    return comparison


def verbatim_sha256(task_dir: Path) -> str:
    load_verbatim_messages(task_dir)
    return canonical_json_sha256(verbatim_path(task_dir))


def git_candidate_state(repository: Path) -> str:
    """Digest the publishable committed and uncommitted Git-visible candidate."""
    try:
        state = write_admission.git_write_state(repository)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError(f"cannot resolve current Git candidate for {repository}") from exc
    return write_admission.state_digest(state)


def _admitted_review(task_dir: Path, value: dict, stage: str) -> dict:
    """Resolve the admission and actor for the latest run of this review stage."""
    admission_id = value.get("review_admission_id")
    if not isinstance(admission_id, str) or not admission_id:
        raise ValueError(f"{stage} product review has no admitted review identity")
    admissions = review_admission.admissions(task_dir)
    admission = next(
        (entry for entry in admissions if entry.get("admission_id") == admission_id),
        None,
    )
    classification = (
        admission.get("classification") if isinstance(admission, dict) else None
    )
    pair = admission.get("pair") if isinstance(admission, dict) else None
    if (
        not isinstance(admission, dict)
        or admission.get("decision") != "admitted_review"
        or not isinstance(classification, dict)
        or classification.get("work_class") != review_admission.REVIEW
        or admission.get("review_kind") != stage
        or not isinstance(pair, dict)
    ):
        raise ValueError(f"{stage} product review was not produced by an admitted reviewer")
    latest_stage_admission = next(
        (
            entry
            for entry in reversed(admissions)
            if entry.get("decision") == "admitted_review"
            and entry.get("review_kind") == stage
        ),
        None,
    )
    if latest_stage_admission is not admission:
        raise ValueError(
            f"{stage} product review was superseded by a newer review of that stage"
        )
    reviewer = value.get("reviewer")
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("runner") != pair.get("reviewer_runner")
        or reviewer.get("family") != pair.get("reviewer_family")
    ):
        raise ValueError(f"{stage} reviewer differs from the admitted reviewer family")
    if stage == "statement":
        author_family = pair.get("author_family")
        if (
            author_family not in {"Codex", "Claude"}
            or author_family == pair.get("reviewer_family")
        ):
            raise ValueError("statement review admission has no independent author pair")
    return admission


def current_statement_digests(task_dir: Path) -> dict[str, str | None]:
    task = task_dir / "task.md"
    contract = task_dir / "task_contract.json"
    return {
        "task_sha256": statement_sha256(task),
        "contract_sha256": canonical_json_sha256(contract) if contract.is_file() else None,
    }


def load_result(task_dir: Path, stage: str) -> dict:
    path = result_path(task_dir, stage)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"product-review result is missing or unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("product-review result must be a JSON object")
    return value


def validate_result(
    task_dir: Path, stage: str, *, require_passing: bool = True
) -> tuple[bool, str, dict | None]:
    """Validate one fresh reviewer decision against the bytes it claims."""
    try:
        value = load_result(task_dir, stage)
        if value.get("schema_version") != SCHEMA_VERSION or value.get("stage") != stage:
            raise ValueError("product-review result schema or stage does not match")
        verdict = value.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"product-review verdict is unknown: {verdict!r}")
        if require_passing and verdict != PASSING_VERDICT:
            raise ValueError(
                f"product-review verdict is {verdict!r}, not {PASSING_VERDICT!r}"
            )
        conclusion = value.get("conclusion_ru")
        if not isinstance(conclusion, str) or not conclusion.strip():
            raise ValueError("product-review result has no concise Russian conclusion")
        reviewer = value.get("reviewer")
        if not isinstance(reviewer, dict):
            raise ValueError("product-review result has no reviewer identity")
        if reviewer.get("family") not in {"Codex", "Claude"}:
            raise ValueError("product-review reviewer must be Codex or Claude")
        review_admission_record = _admitted_review(task_dir, value, stage)
        comparison_verbatim = verbatim_record_for_result(task_dir, stage, value)
        validate_requirement_comparison(
            comparison_verbatim, value, stage, require_passing=require_passing
        )
        packet_name = value.get("packet")
        if not isinstance(packet_name, str) or not packet_name:
            raise ValueError("product-review packet has no task-relative path")
        packet = (task_dir / packet_name).resolve()
        if task_relative_path(task_dir, packet) != packet_name:
            raise ValueError("product-review packet must use its normalized task-relative path")
        if not packet.is_file() or value.get("packet_sha256") != file_sha256(packet):
            raise ValueError("product-review packet changed after the verdict")
        if stage == "statement":
            for name, digest in current_statement_digests(task_dir).items():
                if value.get(name) != digest:
                    raise ValueError(f"{name} changed after statement review")
        report = report_path(task_dir, stage)
        if not report.is_file() or report.stat().st_size == 0:
            raise ValueError("product-review HTML report is missing or empty")
        if value.get("report_sha256") != file_sha256(report):
            raise ValueError("product-review HTML report changed after the verdict")
        if stage == "statement":
            statement = (task_dir / "task.md").read_text(encoding="utf-8")
            if not report_contains_readable_statement(report, statement):
                raise ValueError(
                    "statement-review HTML does not contain the complete normalized "
                    "statement as one visible readable section"
                )
        else:
            candidates = value.get("candidate_states")
            if not isinstance(candidates, dict) or not candidates:
                raise ValueError("completion review has no exact Git candidate states")
            access_profile = review_admission_record.get("access_profile")
            directories = (
                access_profile.get("target_repositories")
                if isinstance(access_profile, dict)
                else None
            )
            if not isinstance(directories, list) or not directories:
                raise ValueError("completion review admission has no repository grant")
            expected_repositories = {
                str(Path(str(path)).resolve()) for path in directories
            }
            if set(candidates) != expected_repositories:
                raise ValueError(
                    "completion candidate trees differ from the exact reviewer grant"
                )
            for raw_path, expected_state in candidates.items():
                if not isinstance(raw_path, str) or not isinstance(expected_state, str):
                    raise ValueError("completion candidate state record is malformed")
                repository = Path(raw_path)
                if (
                    not repository.is_absolute()
                    or git_candidate_state(repository) != expected_state
                ):
                    raise ValueError(f"completion candidate changed after review: {raw_path}")
        reviewed_at = value.get("reviewed_at")
        if not isinstance(reviewed_at, str) or not reviewed_at.strip():
            raise ValueError("product-review result has no reviewed_at timestamp")
    except (OSError, ValueError) as exc:
        return False, str(exc), None
    verdict_detail = "satisfied" if value["verdict"] == PASSING_VERDICT else "valid"
    return True, f"current {stage} product review is digest-bound and {verdict_detail}", value
