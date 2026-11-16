"""`plan` surfaces applicable lessons (v2.6 LESSON-04 / CARD-L04).

A lesson nobody can see before running is a lesson nobody follows. The plan is
still read-only: it must list lessons and never evaluate a predicate.
"""
from __future__ import annotations

import json

import pytest

from shortdrama import cli
from shortdrama.driver import Driver


@pytest.fixture
def lesson_file(tmp_path):
    path = tmp_path / "repository.yml"
    path.write_text(
        "version: agentgate.io/lessons/v1\n"
        "scope: repository\n"
        "lessons:\n"
        "  - id: test.assembly.only\n"
        "    enforcement: soft\n"
        "    status: pending\n"
        "    applies_to_stages: [assembly]\n"
        "    trigger: t\n"
        "    risk: r\n"
        "    fix: do not do it again\n"
        "    regression: incident\n"
        "  - id: test.everywhere\n"
        "    enforcement: soft\n"
        "    status: pending\n"
        "    trigger: t\n"
        "    risk: r\n"
        "    fix: always\n"
        "    regression: incident\n",
        encoding="utf-8",
    )
    return path


def test_plan_lists_scoped_and_unscoped_lessons(lesson_file, monkeypatch):
    monkeypatch.setenv("LESSONS_PATH", str(lesson_file))
    plan = Driver("demo", "ep01").plan()
    ids = {lesson["id"] for lesson in plan["applicable_lessons"]}
    assert ids == {"test.assembly.only", "test.everywhere"}


def test_plan_reports_the_stages_a_lesson_touches(lesson_file, monkeypatch):
    monkeypatch.setenv("LESSONS_PATH", str(lesson_file))
    plan = Driver("demo", "ep01").plan()
    scoped = next(l for l in plan["applicable_lessons"] if l["id"] == "test.assembly.only")
    assert scoped["stages"] == ["assembly"]
    unscoped = next(l for l in plan["applicable_lessons"] if l["id"] == "test.everywhere")
    assert len(unscoped["stages"]) == 9


def test_plan_never_evaluates_a_predicate(lesson_file, monkeypatch):
    # No primitive is registered in P0; a hard lesson would fail to load rather
    # than be evaluated. Soft lessons carry no predicate at all.
    monkeypatch.setenv("LESSONS_PATH", str(lesson_file))
    plan = Driver("demo", "ep01").plan()
    assert all("predicate" not in lesson for lesson in plan["applicable_lessons"])


def test_plan_with_empty_lessons_returns_empty_list(tmp_path, monkeypatch):
    path = tmp_path / "empty.yml"
    path.write_text("version: agentgate.io/lessons/v1\nlessons: []\n", encoding="utf-8")
    monkeypatch.setenv("LESSONS_PATH", str(path))
    plan = Driver("demo", "ep01").plan()
    assert plan["applicable_lessons"] == []


def test_cli_plan_prints_applicable_lessons(lesson_file, monkeypatch, capsys):
    monkeypatch.setenv("LESSONS_PATH", str(lesson_file))
    code = cli.main(["plan", "--project", "demo", "--episode", "ep01", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "applicable_lessons" in payload
    assert len(payload["applicable_lessons"]) == 2