"""Digest and repository bindings for the two product-review boundaries."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import product_review  # noqa: E402


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def write_verbatim(task: Path, text: str = "verbatim user intent") -> str:
    write_json(
        product_review.verbatim_path(task),
        {
            "schema_version": 1,
            "messages": [{
                "channel": "cli",
                "source_id": "message-1",
                "occurred_at": "2026-08-26T00:00:00+00:00",
                "text": text,
            }],
        },
    )
    return product_review.verbatim_sha256(task)


def comparison(observed: str = "the statement/result does the exact job") -> list[dict]:
    return [{
        "source_ids": ["message-1"],
        "requirement": "verbatim user intent",
        "observed_result": observed,
        "outcome": "satisfied",
    }]


def admitted_review(
    task: Path,
    *,
    reviewer: str = "claude",
    author: str = "codex",
    stage: str = "statement",
    target_repositories: list[str] | None = None,
) -> dict:
    families = {"codex": "Codex", "claude": "Claude"}
    admission_id = f"admission-{stage}-{reviewer}-{author}"
    entry = {
        "schema_version": 1,
        "admission_id": admission_id,
        "decision": "admitted_review",
        "review_kind": stage,
        "classification": {"work_class": "review"},
        "pair": {
            "reviewer_runner": reviewer,
            "reviewer_family": families[reviewer],
            "author_runner": author,
            "author_family": families[author],
        },
        "access_profile": {
            "role": "reviewer",
            "sandbox_mode": "read-only",
            "target_repositories": target_repositories or [],
            "grants_write": False,
        },
    }
    ledger = task / "reviews" / "admissions.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    runner_path = task / ".runner" / "runner.json"
    runner = (
        json.loads(runner_path.read_text(encoding="utf-8"))
        if runner_path.exists()
        else {}
    )
    runner.update({"review_kind": stage, "review_admission": entry})
    write_json(runner_path, runner)
    return {
        "review_admission_id": admission_id,
        "reviewer": {"runner": reviewer, "family": families[reviewer]},
    }


def statement_task(tmp_path: Path) -> tuple[Path, Path]:
    task = tmp_path / "001-example"
    task.mkdir()
    (task / "task.md").write_text("verbatim user intent\n", encoding="utf-8")
    (task / "task_contract.json").write_text('{"version": 1}\n', encoding="utf-8")
    verbatim_digest = write_verbatim(task)
    packet = task / "statement-packet.json"
    packet.write_text('{"verbatim_user_intent":"verbatim user intent"}\n', encoding="utf-8")
    report = product_review.report_path(task, "statement")
    report.parent.mkdir(parents=True)
    statement = (task / "task.md").read_text(encoding="utf-8")
    report.write_text(
        f"<html><main><p>{statement}</p></main><p>verdict</p></html>\n",
        encoding="utf-8",
    )
    digests = product_review.current_statement_digests(task)
    write_json(
        product_review.result_path(task, "statement"),
        {
            "schema_version": 1,
            "stage": "statement",
            "verdict": "satisfied",
            "conclusion_ru": "Постановка соответствует запросу пользователя.",
            "reviewed_at": "2026-08-26T00:00:00+00:00",
            "packet": packet.name,
            "packet_sha256": product_review.file_sha256(packet),
            "report_sha256": product_review.file_sha256(report),
            "verbatim_user_words_sha256": verbatim_digest,
            "requirement_comparison": comparison(),
            **digests,
            **admitted_review(task),
        },
    )
    return task, packet


def test_statement_result_is_invalidated_by_material_statement_change(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    assert product_review.validate_result(task, "statement")[0]
    (task / "task.md").write_text("changed user intent\n", encoding="utf-8")
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "task_sha256 changed" in detail


def test_statement_packet_in_task_subdirectory_is_accepted(tmp_path: Path) -> None:
    task, packet = statement_task(tmp_path)
    nested = task / "reviews" / packet.name
    nested.parent.mkdir(exist_ok=True)
    packet.replace(nested)
    result_path = product_review.result_path(task, "statement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["packet"] = "reviews/statement-packet.json"
    write_json(result_path, result)

    passed, detail, _result = product_review.validate_result(task, "statement")

    assert passed, detail


def test_statement_packet_cannot_escape_task_directory(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    result_path = product_review.result_path(task, "statement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["packet"] = "../outside.json"
    write_json(result_path, result)

    passed, detail, _result = product_review.validate_result(task, "statement")

    assert not passed
    assert "inside the task directory" in detail


def test_non_passing_review_is_valid_for_delivery_but_not_for_the_gate(
    tmp_path: Path,
) -> None:
    task, _packet = statement_task(tmp_path)
    result_path = product_review.result_path(task, "statement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["verdict"] = "not_satisfied"
    result["requirement_comparison"][0].update({
        "observed_result": "the statement omits the requirement",
        "outcome": "not_satisfied",
    })
    write_json(result_path, result)

    assert product_review.validate_result(
        task, "statement", require_passing=False
    )[0]
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "not 'satisfied'" in detail


def test_statement_digest_ignores_lifecycle_frontmatter_but_not_body(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    statement = task / "task.md"
    statement.write_text(
        '---\nid: 1\nstatus: "planned"\nstatus_detail: "waiting"\n---\n# Exact job\n',
        encoding="utf-8",
    )
    before = product_review.current_statement_digests(task)["task_sha256"]
    statement.write_text(
        '---\nid: 1\nstatus: "blocked"\nstatus_detail: "review_waiting"\n---\n# Exact job\n',
        encoding="utf-8",
    )
    assert product_review.current_statement_digests(task)["task_sha256"] == before
    statement.write_text(
        '---\nid: 1\nstatus: "blocked"\n---\n# Exact job\n\n## Status\n- round 16\n\n## Related Tasks\n- 2\n',
        encoding="utf-8",
    )
    with_status = product_review.current_statement_digests(task)["task_sha256"]
    statement.write_text(
        '---\nid: 1\nstatus: "blocked"\n---\n# Exact job\n\n## Status\n- round 17\n\n## Related Tasks\n- 2\n',
        encoding="utf-8",
    )
    assert product_review.current_statement_digests(task)["task_sha256"] == with_status
    statement.write_text(
        '---\nid: 1\nstatus: "blocked"\nstatus_detail: "review_waiting"\n---\n# Different job\n',
        encoding="utf-8",
    )
    assert product_review.current_statement_digests(task)["task_sha256"] != before


def test_creating_first_terminal_status_section_does_not_change_statement_digest(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    statement = task / "task.md"
    statement.write_text("# Exact job\n", encoding="utf-8")
    before = product_review.statement_sha256(statement)

    statement.write_text("# Exact job\n\n## Status\n- round 1\n", encoding="utf-8")

    assert product_review.statement_sha256(statement) == before


def test_creating_first_middle_status_section_does_not_change_statement_digest(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    statement = task / "task.md"
    statement.write_text("# Exact job\n\n## Related Tasks\n- 2\n", encoding="utf-8")
    before = product_review.statement_sha256(statement)

    statement.write_text(
        "# Exact job\n\n## Status\n- round 1\n\n## Related Tasks\n- 2\n",
        encoding="utf-8",
    )

    assert product_review.statement_sha256(statement) == before


def test_statement_result_requires_complete_normalized_visible_statement_text(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    report = product_review.report_path(task, "statement")
    report.write_text(
        "<html><main>truncated statement</main></html>\n",
        encoding="utf-8",
    )
    result_path = product_review.result_path(task, "statement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["report_sha256"] = product_review.file_sha256(report)
    write_json(result_path, result)
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "complete normalized statement" in detail


def test_readable_statement_does_not_require_a_raw_markdown_duplicate(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    (task / "task.md").write_text(
        "---\nid: 1\nstatus: planned\n---\n# Заголовок\n\n- Полный **текст** [постановки](https://example.test).\n",
        encoding="utf-8",
    )
    report = product_review.report_path(task, "statement")
    report.write_text(
        "<html><head><title>Не считается</title></head><body><h1>Заголовок</h1>"
        "<ul><li>Полный <strong>текст</strong> постановки.</li></ul></body></html>\n",
        encoding="utf-8",
    )
    assert product_review.report_contains_readable_statement(
        report, (task / "task.md").read_text(encoding="utf-8")
    )


def test_readable_statement_accepts_rendered_ordered_list_markers(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    (task / "task.md").write_text(
        "# Проверка\n\n1. Первый полный пункт.\n2. Второй полный пункт.\n",
        encoding="utf-8",
    )
    report = product_review.report_path(task, "statement")
    report.write_text(
        "<html><body><h1>Проверка</h1><ol>"
        "<li>Первый полный пункт.</li><li>Второй полный пункт.</li>"
        "</ol></body></html>\n",
        encoding="utf-8",
    )

    assert product_review.report_contains_readable_statement(
        report, (task / "task.md").read_text(encoding="utf-8")
    )


def test_readable_statement_requires_indented_numeric_continuation(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    (task / "task.md").write_text(
        "# Проверка\n\n1. Решение подтверждено и принято\n"
        "   1280. Полный пункт продолжается.\n",
        encoding="utf-8",
    )
    report = product_review.report_path(task, "statement")
    report.write_text(
        "<html><body><h1>Проверка</h1><ol>"
        "<li>Решение подтверждено и принято. Полный пункт продолжается.</li>"
        "</ol></body></html>\n",
        encoding="utf-8",
    )

    assert not product_review.report_contains_readable_statement(
        report, (task / "task.md").read_text(encoding="utf-8")
    )


def test_readable_statement_ignores_lifecycle_status_journal(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    statement = task / "task.md"
    statement.write_text(
        "---\nid: 1\nstatus: blocked\n---\n# Exact job\n\n"
        "## Status\n- round 22\n\n## Related Tasks\n- 2\n",
        encoding="utf-8",
    )
    report = product_review.report_path(task, "statement")
    report.write_text(
        "<html><body><h1>Exact job</h1><h2>Related Tasks</h2><p>2</p></body></html>\n",
        encoding="utf-8",
    )

    assert product_review.report_contains_readable_statement(
        report, statement.read_text(encoding="utf-8")
    )

    statement.write_text(
        statement.read_text(encoding="utf-8").replace("round 22", "round 23"),
        encoding="utf-8",
    )
    assert product_review.report_contains_readable_statement(
        report, statement.read_text(encoding="utf-8")
    )


def test_excluded_verbatim_message_requires_a_reason_and_is_not_a_requirement(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    write_verbatim(task)
    value = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    value["excluded_messages"] = [{
        "channel": "gmail",
        "source_id": "other-topic",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Комментарий о другом канале",
        "reason": "Относится только к Telegram, а не к письмам этой задачи.",
    }]
    write_json(product_review.verbatim_path(task), value)
    assert [item["source_id"] for item in product_review.load_verbatim_messages(task)] == [
        "message-1"
    ]
    value["excluded_messages"][0]["reason"] = ""
    write_json(product_review.verbatim_path(task), value)
    try:
        product_review.load_verbatim_messages(task)
    except ValueError as exc:
        assert "must name the reason" in str(exc)
    else:
        raise AssertionError("excluded message without a reason was accepted")


def test_comparison_covers_excluded_messages_and_keeps_the_reason_visible(
    tmp_path: Path,
) -> None:
    task, _packet = statement_task(tmp_path)
    verbatim = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    verbatim["excluded_messages"] = [{
        "channel": "gmail",
        "source_id": "other-topic",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Комментарий о другом канале",
        "reason": "Относится только к Telegram, а не к письмам этой задачи.",
    }]
    write_json(product_review.verbatim_path(task), verbatim)
    result_path = product_review.result_path(task, "statement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["verbatim_user_words_sha256"] = product_review.verbatim_sha256(task)
    result["requirement_comparison"].append({
        "source_ids": ["other-topic"],
        "requirement": "Проверить, относится ли комментарий к этой работе",
        "observed_result": "Проверяющий подтвердил, что это другой канал",
        "outcome": "out_of_scope",
        "reason": "Относится только к Telegram, а не к письмам этой задачи.",
    })
    write_json(result_path, result)
    assert product_review.validate_result(task, "statement")[0]

    result["requirement_comparison"][-1]["reason"] = "Подменённая причина."
    write_json(result_path, result)
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "preserve the recorded exclusion reason" in detail
    result["requirement_comparison"][-1]["reason"] = verbatim["excluded_messages"][0][
        "reason"
    ]

    result["requirement_comparison"].pop()
    write_json(result_path, result)
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "does not cover every user message" in detail


def test_challenged_exclusion_names_the_failing_requirement(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    verbatim = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    verbatim["excluded_messages"] = [{
        "channel": "gmail",
        "source_id": "hidden-requirement",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "This belongs in the current task",
        "reason": "Previously assigned elsewhere.",
    }]
    result = {
        "requirement_comparison": [{
            "source_ids": ["message-1"],
            "requirement": "verbatim user intent",
            "observed_result": "covered",
            "outcome": "satisfied",
        }, {
            "source_ids": ["hidden-requirement"],
            "requirement": "This belongs in the current task",
            "observed_result": "missing from the statement",
            "outcome": "not_satisfied",
        }],
    }
    try:
        product_review.validate_requirement_comparison(verbatim, result, "statement")
    except ValueError as exc:
        assert "satisfied verdict conflicts" in str(exc)
    else:
        raise AssertionError("a challenged exclusion was accepted")


def test_completion_allows_a_visible_receipt_without_inventing_a_requirement() -> None:
    verbatim = {
        "messages": [{"source_id": "request"}, {"source_id": "receipt"}],
        "excluded_messages": [],
    }
    result = {
        "requirement_comparison": [
            {
                "source_ids": ["request"],
                "requirement": "Сделать точную работу",
                "observed_result": "Точная работа сделана",
                "outcome": "satisfied",
            },
            {
                "source_ids": ["receipt"],
                "requirement": "Определить, добавляет ли ответ новое требование",
                "observed_result": "Ответ только подтверждает получение",
                "outcome": "not_a_requirement",
                "reason": "В сообщении нет просьбы изменить результат.",
            },
        ]
    }
    rows = product_review.validate_requirement_comparison(
        verbatim, result, "completion"
    )
    assert rows[1]["outcome"] == "not_a_requirement"


def test_included_message_cannot_be_hidden_as_out_of_scope() -> None:
    verbatim = {"messages": [{"source_id": "request"}], "excluded_messages": []}
    result = {
        "requirement_comparison": [{
            "source_ids": ["request"],
            "requirement": "Сделать точную работу",
            "observed_result": "Сообщение отложено",
            "outcome": "out_of_scope",
            "reason": "Неудобное требование",
        }]
    }
    try:
        product_review.validate_requirement_comparison(verbatim, result, "completion")
    except ValueError as exc:
        assert "only excluded_messages" in str(exc)
    else:
        raise AssertionError("included user message was hidden as out_of_scope")


def test_statement_contract_digest_ignores_json_formatting_only(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    assert product_review.validate_result(task, "statement")[0]
    (task / "task_contract.json").write_text(
        '{\n  "version": 1\n}\n', encoding="utf-8"
    )
    assert product_review.validate_result(task, "statement")[0]
    (task / "task_contract.json").write_text(
        '{\n  "version": 2\n}\n', encoding="utf-8"
    )
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "contract_sha256 changed" in detail


def test_verbatim_digest_ignores_json_formatting_only(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    path = product_review.verbatim_path(task)
    before = product_review.verbatim_sha256(task)
    value = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=4, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert product_review.verbatim_sha256(task) == before


def test_authenticated_approval_preserves_its_statement_review(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    result = json.loads(
        product_review.result_path(task, "statement").read_text(encoding="utf-8")
    )
    verbatim = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    verbatim["messages"].append({
        "channel": "gmail",
        "source_id": "approval-message",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Получил, спасибо.",
    })
    write_json(product_review.verbatim_path(task), verbatim)
    after_approval = product_review.verbatim_sha256(task)
    write_json(
        task / "product-review" / "statement-feedback.json",
        {
            "schema_version": 1,
            "records": [{
                "classification": "approval",
                "approval_verified": True,
                "gmail_id": "approval-message",
                "verbatim_text_sha256": hashlib.sha256(
                    "Получил, спасибо.".encode("utf-8")
                ).hexdigest(),
                "verbatim_before_append_sha256": result["verbatim_user_words_sha256"],
                "verbatim_after_append_sha256": after_approval,
                "preserves_review": {
                    "verbatim_user_words_sha256": result["verbatim_user_words_sha256"]
                },
            }],
        },
    )

    passed, detail, _value = product_review.validate_result(task, "statement")

    assert passed, detail
    compared = product_review.verbatim_record_for_result(task, "statement", result)
    assert [message["source_id"] for message in compared["messages"]] == ["message-1"]


def test_approval_cannot_preserve_review_after_earlier_verbatim_text_is_edited(
    tmp_path: Path,
) -> None:
    task, _packet = statement_task(tmp_path)
    result = json.loads(
        product_review.result_path(task, "statement").read_text(encoding="utf-8")
    )
    verbatim = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    verbatim["messages"].append({
        "channel": "gmail",
        "source_id": "approval-message",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Получил, спасибо.",
    })
    write_json(product_review.verbatim_path(task), verbatim)
    after_approval = product_review.verbatim_sha256(task)
    write_json(task / "product-review" / "statement-feedback.json", {
        "schema_version": 1,
        "records": [{
            "classification": "approval",
            "approval_verified": True,
            "gmail_id": "approval-message",
            "verbatim_text_sha256": hashlib.sha256(
                "Получил, спасибо.".encode("utf-8")
            ).hexdigest(),
            "verbatim_before_append_sha256": result["verbatim_user_words_sha256"],
            "verbatim_after_append_sha256": after_approval,
            "target_review": {
                "verbatim_user_words_sha256": result["verbatim_user_words_sha256"]
            },
        }],
    })
    verbatim["messages"][0]["text"] = "rewritten request"
    write_json(product_review.verbatim_path(task), verbatim)

    passed, detail, _value = product_review.validate_result(task, "statement")

    assert not passed
    assert "verbatim user words changed" in detail


def test_verified_approval_label_cannot_preserve_an_objection(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    result = json.loads(
        product_review.result_path(task, "statement").read_text(encoding="utf-8")
    )
    verbatim = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    verbatim["messages"].append({
        "channel": "gmail",
        "source_id": "objection-message",
        "occurred_at": "2026-08-26T01:00:00+00:00",
        "text": "Стоп, это не то, что я просил.",
    })
    write_json(product_review.verbatim_path(task), verbatim)
    write_json(task / "product-review" / "statement-feedback.json", {
        "schema_version": 1,
        "records": [{
            "classification": "approval",
            "approval_verified": True,
            "gmail_id": "objection-message",
            "verbatim_text_sha256": hashlib.sha256(
                "Стоп, это не то, что я просил.".encode("utf-8")
            ).hexdigest(),
            "target_review": {
                "verbatim_user_words_sha256": result["verbatim_user_words_sha256"]
            },
        }],
    })

    passed, detail, _value = product_review.validate_result(task, "statement")

    assert not passed
    assert "verbatim user words changed" in detail


def test_statement_reviewer_must_be_another_family(tmp_path: Path) -> None:
    task, _packet = statement_task(tmp_path)
    path = product_review.result_path(task, "statement")
    result = json.loads(path.read_text(encoding="utf-8"))
    result["reviewer"]["family"] = "Codex"
    write_json(path, result)
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "differs from the admitted reviewer family" in detail


def test_valid_looking_statement_result_without_admitted_review_is_refused(
    tmp_path: Path,
) -> None:
    task, _packet = statement_task(tmp_path)
    (task / "reviews" / "admissions.jsonl").unlink()
    passed, detail, _result = product_review.validate_result(task, "statement")
    assert not passed
    assert "was not produced by an admitted reviewer" in detail


def test_statement_result_cannot_borrow_a_real_technical_review_admission(
    tmp_path: Path,
) -> None:
    task, _packet = statement_task(tmp_path)
    result_path = product_review.result_path(task, "statement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    borrowed = admitted_review(task, stage="technical")
    result.update(borrowed)
    write_json(result_path, result)

    passed, detail, _result = product_review.validate_result(task, "statement")

    assert not passed
    assert "was not produced by an admitted reviewer" in detail


def test_statement_result_cannot_reuse_an_earlier_statement_review_run(
    tmp_path: Path,
) -> None:
    task, _packet = statement_task(tmp_path)
    result_path = product_review.result_path(task, "statement")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    earlier_admission_id = result["review_admission_id"]
    current = admitted_review(task, reviewer="codex", author="claude")
    assert current["review_admission_id"] != earlier_admission_id

    passed, detail, _result = product_review.validate_result(task, "statement")

    assert not passed
    assert "superseded by a newer review" in detail


def test_statement_result_survives_later_author_and_technical_runs(
    tmp_path: Path,
) -> None:
    task, _packet = statement_task(tmp_path)
    admitted_review(task, stage="technical")
    write_json(
        task / ".runner" / "runner.json",
        {"runner": "codex", "workflow": "standard", "access_grant": {}},
    )

    passed, detail, _result = product_review.validate_result(task, "statement")

    assert passed, detail


def test_completion_verdict_is_invalidated_by_later_user_message(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    repository = tmp_path / "candidate"
    states = {str(repository): init_repository(repository)}
    verbatim_digest = write_verbatim(task)
    packet = task / "completion-packet.json"
    packet.write_text("{}\n", encoding="utf-8")
    report = product_review.report_path(task, "completion")
    report.parent.mkdir(parents=True)
    report.write_text("<html>result and verdict</html>\n", encoding="utf-8")
    write_json(task / ".runner" / "runner.json", {
        "access_grant": {"granted_directories": list(states)},
        "review_admission": {"pair": {"reviewer_runner": "claude", "reviewer_family": "Claude"}},
    })
    write_json(product_review.result_path(task, "completion"), {
        "schema_version": 1, "stage": "completion", "verdict": "satisfied",
        "conclusion_ru": "Результат соответствует запросу пользователя.",
        "reviewed_at": "2026-08-26T00:00:00+00:00", "packet": packet.name,
        "packet_sha256": product_review.file_sha256(packet),
        "report_sha256": product_review.file_sha256(report),
        **admitted_review(task, stage="completion", target_repositories=list(states)),
        "candidate_states": states, "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": comparison(),
    })
    assert product_review.validate_result(task, "completion")[0]
    write_json(
        task / ".runner" / "runner.json",
        {
            "runner": "codex",
            "workflow": "standard",
            "access_grant": {"granted_directories": list(states)},
        },
    )
    passed, detail, _ = product_review.validate_result(task, "completion")
    assert passed, detail
    value = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    value["messages"].append({
        "channel": "gmail", "source_id": "message-2",
        "occurred_at": "2026-08-26T01:00:00+00:00", "text": "later clarification",
    })
    write_json(product_review.verbatim_path(task), value)
    passed, detail, _ = product_review.validate_result(task, "completion")
    assert not passed
    assert "changed after completion review" in detail


def test_authenticated_approval_preserves_completion_review(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    repository = tmp_path / "candidate"
    states = {str(repository): init_repository(repository)}
    verbatim_digest = write_verbatim(task)
    packet = task / "completion-packet.json"
    packet.write_text("{}\n", encoding="utf-8")
    report = product_review.report_path(task, "completion")
    report.parent.mkdir(parents=True)
    report.write_text("<html>result and verdict</html>\n", encoding="utf-8")
    write_json(product_review.result_path(task, "completion"), {
        "schema_version": 1, "stage": "completion", "verdict": "satisfied",
        "conclusion_ru": "Результат соответствует запросу пользователя.",
        "reviewed_at": "2026-08-26T00:00:00+00:00", "packet": packet.name,
        "packet_sha256": product_review.file_sha256(packet),
        "report_sha256": product_review.file_sha256(report),
        **admitted_review(task, stage="completion", target_repositories=list(states)),
        "candidate_states": states, "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": comparison(),
    })
    value = json.loads(product_review.verbatim_path(task).read_text(encoding="utf-8"))
    value["messages"].append({
        "channel": "gmail", "source_id": "approval-message",
        "occurred_at": "2026-08-26T01:00:00+00:00", "text": "Получил, спасибо.",
    })
    write_json(product_review.verbatim_path(task), value)
    after_approval = product_review.verbatim_sha256(task)
    write_json(task / "product-review" / "completion-feedback.json", {
        "schema_version": 1,
        "records": [{
            "classification": "approval",
            "approval_verified": True,
            "gmail_id": "approval-message",
            "verbatim_text_sha256": hashlib.sha256(
                "Получил, спасибо.".encode("utf-8")
            ).hexdigest(),
            "verbatim_before_append_sha256": verbatim_digest,
            "verbatim_after_append_sha256": after_approval,
            "target_review": {"verbatim_user_words_sha256": verbatim_digest},
        }],
    })

    passed, detail, _ = product_review.validate_result(task, "completion")

    assert passed, detail


def test_goal_drift_cannot_pass_even_when_candidate_matches_statement(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    repository = tmp_path / "candidate"
    states = {str(repository): init_repository(repository)}
    verbatim_digest = write_verbatim(task)
    packet = task / "completion-packet.json"
    packet.write_text('{"derived_statement":"implemented exactly"}\n', encoding="utf-8")
    report = product_review.report_path(task, "completion")
    report.parent.mkdir(parents=True)
    report.write_text("<html>candidate matches statement but missed user intent</html>\n", encoding="utf-8")
    write_json(task / ".runner" / "runner.json", {
        "access_grant": {"granted_directories": list(states)},
        "review_admission": {"pair": {"reviewer_runner": "claude", "reviewer_family": "Claude"}},
    })
    write_json(product_review.result_path(task, "completion"), {
        "schema_version": 1, "stage": "completion", "verdict": "satisfied",
        "conclusion_ru": "Постановка выполнена, но исходная просьба нет.",
        "reviewed_at": "2026-08-26T00:00:00+00:00", "packet": packet.name,
        "packet_sha256": product_review.file_sha256(packet),
        "report_sha256": product_review.file_sha256(report),
        **admitted_review(task, stage="completion", target_repositories=list(states)),
        "candidate_states": states, "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": [{
            **comparison()[0], "observed_result": "only the derived statement matches",
            "outcome": "not_satisfied",
        }],
    })
    passed, detail, _ = product_review.validate_result(task, "completion")
    assert not passed
    assert "conflicts with the verbatim requirement comparison" in detail


def init_repository(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "value.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "value.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "one"], check=True)
    return product_review.git_candidate_state(path)


def test_completion_result_binds_every_exact_reviewer_repository(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    states = {str(first): init_repository(first), str(second): init_repository(second)}
    verbatim_digest = write_verbatim(task)
    packet = task / "completion-packet.json"
    packet.write_text("{}\n", encoding="utf-8")
    report = product_review.report_path(task, "completion")
    report.parent.mkdir(parents=True)
    report.write_text("<html>result and verdict</html>\n", encoding="utf-8")
    write_json(
        task / ".runner" / "runner.json",
        {
            "access_grant": {"granted_directories": list(states)},
            "review_admission": {
                "pair": {"reviewer_runner": "claude", "reviewer_family": "Claude"}
            },
        },
    )
    result = {
        "schema_version": 1,
        "stage": "completion",
        "verdict": "satisfied",
        "conclusion_ru": "Результат соответствует запросу пользователя.",
        "reviewed_at": "2026-08-26T00:00:00+00:00",
        "packet": packet.name,
        "packet_sha256": product_review.file_sha256(packet),
        "report_sha256": product_review.file_sha256(report),
        **admitted_review(task, stage="completion", target_repositories=list(states)),
        "candidate_states": states,
        "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": comparison(),
    }
    write_json(product_review.result_path(task, "completion"), result)
    assert product_review.validate_result(task, "completion")[0]

    write_json(
        task / ".runner" / "runner.json",
        {"access_grant": {"granted_directories": [str(first)]}},
    )
    passed, detail, _result = product_review.validate_result(task, "completion")
    assert passed, detail

    result["candidate_states"].pop(str(second))
    write_json(product_review.result_path(task, "completion"), result)
    passed, detail, _result = product_review.validate_result(task, "completion")
    assert not passed
    assert "exact reviewer grant" in detail


def test_completion_result_is_invalidated_by_uncommitted_candidate_change(
    tmp_path: Path,
) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    repository = tmp_path / "candidate"
    states = {str(repository): init_repository(repository)}
    verbatim_digest = write_verbatim(task)
    packet = task / "completion-packet.json"
    packet.write_text("{}\n", encoding="utf-8")
    report = product_review.report_path(task, "completion")
    report.parent.mkdir(parents=True)
    report.write_text("<html>result and verdict</html>\n", encoding="utf-8")
    write_json(task / ".runner" / "runner.json", {
        "access_grant": {"granted_directories": list(states)},
    })
    write_json(product_review.result_path(task, "completion"), {
        "schema_version": 1, "stage": "completion", "verdict": "satisfied",
        "conclusion_ru": "Результат соответствует запросу пользователя.",
        "reviewed_at": "2026-08-26T00:00:00+00:00", "packet": packet.name,
        "packet_sha256": product_review.file_sha256(packet),
        "report_sha256": product_review.file_sha256(report),
        **admitted_review(task, stage="completion", target_repositories=list(states)),
        "candidate_states": states,
        "verbatim_user_words_sha256": verbatim_digest,
        "requirement_comparison": comparison(),
    })
    assert product_review.validate_result(task, "completion")[0]

    (repository / "value.txt").write_text("rewritten\n", encoding="utf-8")
    (repository / "new_module.py").write_text("candidate = True\n", encoding="utf-8")
    passed, detail, _result = product_review.validate_result(task, "completion")
    assert not passed
    assert "completion candidate changed after review" in detail


def test_completion_result_requires_the_admitted_reviewer_family(tmp_path: Path) -> None:
    task = tmp_path / "001-example"
    task.mkdir()
    repository = tmp_path / "candidate"
    states = {str(repository): init_repository(repository)}
    verbatim_digest = write_verbatim(task)
    packet = task / "completion-packet.json"
    packet.write_text("{}\n", encoding="utf-8")
    report = product_review.report_path(task, "completion")
    report.parent.mkdir(parents=True)
    report.write_text("<html>result and verdict</html>\n", encoding="utf-8")
    write_json(
        task / ".runner" / "runner.json",
        {
            "access_grant": {"granted_directories": list(states)},
            "review_admission": {
                "pair": {"reviewer_runner": "claude", "reviewer_family": "Claude"}
            },
        },
    )
    write_json(
        product_review.result_path(task, "completion"),
        {
            "schema_version": 1,
            "stage": "completion",
            "verdict": "satisfied",
            "conclusion_ru": "Результат соответствует запросу пользователя.",
            "reviewed_at": "2026-08-26T00:00:00+00:00",
            "packet": packet.name,
            "packet_sha256": product_review.file_sha256(packet),
            "report_sha256": product_review.file_sha256(report),
            **admitted_review(task, stage="completion", target_repositories=list(states)),
            "reviewer": {"runner": "codex", "family": "Codex"},
            "candidate_states": states,
            "verbatim_user_words_sha256": verbatim_digest,
            "requirement_comparison": comparison(),
        },
    )
    passed, detail, _result = product_review.validate_result(task, "completion")
    assert not passed
    assert "admitted reviewer family" in detail
