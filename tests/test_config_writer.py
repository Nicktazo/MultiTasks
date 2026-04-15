"""Tests for core/config_writer.py — round-trip, mutations, validation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest
import yaml

from core.config import load_config, Settings
from core.config_writer import (
    config_to_dict,
    apply_mutation,
    _mutate_update_project,
    _mutate_delete_project,
    _mutate_upsert_task,
    _mutate_delete_task,
    _mutate_settings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=path, check=True, capture_output=True,
    )


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp(prefix="mt_cw_test_")
    for name in ("projA", "projB"):
        _make_git_repo(os.path.join(tmp, name))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _write_config(tmp: str, data: dict) -> str:
    path = os.path.join(tmp, "projects.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return path


def _base_config(tmp: str) -> dict:
    return {
        "projects": {
            "projA": {
                "path": os.path.join(tmp, "projA"),
                "tasks": [
                    {"id": "t1", "prompt": "Task 1", "tool": "claude"},
                    {"id": "t2", "prompt": "Review t1", "tool": "codex-review",
                     "review_of": "t1", "depends_on": ["t1"]},
                ],
            },
            "projB": {
                "path": os.path.join(tmp, "projB"),
                "tasks": [
                    {"id": "b1", "prompt": "Task b1", "tool": "claude",
                     "depends_on": ["projA:t1"]},
                ],
            },
        },
        "settings": {
            "max_parallel": 2, "timeout": 600, "notify": "none",
            "dashboard_port": 8704,
            "state_dir": os.path.join(tmp, "state"),
            "log_dir": os.path.join(tmp, "logs"),
            "dirty_workspace": "warn",
        },
    }


# ---------------------------------------------------------------------------
# Test 1: config_to_dict round-trip
# ---------------------------------------------------------------------------

def test_config_to_dict_roundtrip(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)
    config = load_config(cfg_path)
    result = config_to_dict(config)

    # Projects preserved
    assert list(result["projects"].keys()) == ["projA", "projB"]
    # Tasks preserved
    assert len(result["projects"]["projA"]["tasks"]) == 2
    assert result["projects"]["projA"]["tasks"][0]["id"] == "t1"
    # Same-project review_of uses local ID
    t2 = result["projects"]["projA"]["tasks"][1]
    assert t2["review_of"] == "t1"
    assert "t1" in t2.get("depends_on", [])
    # Cross-project dep uses global ID
    b1 = result["projects"]["projB"]["tasks"][0]
    assert "projA:t1" in b1.get("depends_on", [])
    # Settings keys in fixed order
    assert list(result["settings"].keys()) == [
        "max_parallel", "timeout", "notify", "dashboard_port",
        "state_dir", "log_dir", "dirty_workspace", "public_base_url",
        "listen_address",
    ]


# ---------------------------------------------------------------------------
# Test 2: apply_mutation — upsert task to existing project
# ---------------------------------------------------------------------------

def test_upsert_task_existing_project(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    result = apply_mutation(cfg_path, _mutate_upsert_task, {
        "project": "projA",
        "id": "t3",
        "prompt": "New task",
        "tool": "claude",
    })

    assert result["ok"] is True
    tasks = result["config"]["projects"]["projA"]["tasks"]
    assert any(t["id"] == "t3" for t in tasks)


# ---------------------------------------------------------------------------
# Test 3: apply_mutation — upsert task creates new project
# ---------------------------------------------------------------------------

def test_upsert_task_creates_project(tmp_workspace):
    _make_git_repo(os.path.join(tmp_workspace, "projC"))
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    result = apply_mutation(cfg_path, _mutate_upsert_task, {
        "project": "projC",
        "project_path": os.path.join(tmp_workspace, "projC"),
        "id": "c1",
        "prompt": "First task in C",
        "tool": "claude",
    })

    assert result["ok"] is True
    assert "projC" in result["config"]["projects"]


# ---------------------------------------------------------------------------
# Test 4: upsert task without project_path for new project → error
# ---------------------------------------------------------------------------

def test_upsert_task_new_project_no_path(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    result = apply_mutation(cfg_path, _mutate_upsert_task, {
        "project": "projC",
        "id": "c1",
        "prompt": "Task",
        "tool": "claude",
    })

    assert result["ok"] is False
    assert any("project_path" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Test 5: delete task blocked by reference
# ---------------------------------------------------------------------------

def test_delete_task_blocked_by_reference(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    # t1 is referenced by t2 (review_of + depends_on) and projB:b1 (depends_on)
    result = apply_mutation(cfg_path, _mutate_delete_task, {
        "project": "projA", "id": "t1",
    })

    assert result["ok"] is False
    assert any("referenced by" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Test 6: delete task succeeds when no references
# ---------------------------------------------------------------------------

def test_delete_task_no_refs(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    # b1 has no downstream references — safe to delete
    # But we need to also remove the cross-project dep from projA:t1 first
    # Actually b1 is a leaf — nothing depends on it
    result = apply_mutation(cfg_path, _mutate_delete_task, {
        "project": "projB", "id": "b1",
    })

    assert result["ok"] is True
    # projB had only one task, so the whole project should be deleted
    assert "projB" not in result["config"]["projects"]


# ---------------------------------------------------------------------------
# Test 7: delete project blocked by external references
# ---------------------------------------------------------------------------

def test_delete_project_blocked_by_refs(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    # projA is referenced by projB:b1 (depends_on projA:t1)
    result = apply_mutation(cfg_path, _mutate_delete_project, {"name": "projA"})

    assert result["ok"] is False
    assert any("Cannot delete" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Test 8: delete project succeeds when no external refs
# ---------------------------------------------------------------------------

def test_delete_project_no_refs(tmp_workspace):
    data = _base_config(tmp_workspace)
    # Remove cross-project dep so projB has no external references TO projA
    data["projects"]["projB"]["tasks"][0]["depends_on"] = []
    cfg_path = _write_config(tmp_workspace, data)

    result = apply_mutation(cfg_path, _mutate_delete_project, {"name": "projB"})

    assert result["ok"] is True
    assert "projB" not in result["config"]["projects"]


# ---------------------------------------------------------------------------
# Test 9: can delete last project (empty projects dict is valid)
# ---------------------------------------------------------------------------

def test_can_delete_last_project(tmp_workspace):
    data = {
        "projects": {
            "only": {
                "path": os.path.join(tmp_workspace, "projA"),
                "tasks": [{"id": "t1", "prompt": "Solo", "tool": "claude"}],
            },
        },
        "settings": {
            "max_parallel": 1, "timeout": 600, "notify": "none",
            "dashboard_port": 8704,
            "state_dir": os.path.join(tmp_workspace, "state"),
            "log_dir": os.path.join(tmp_workspace, "logs"),
            "dirty_workspace": "warn",
        },
    }
    cfg_path = _write_config(tmp_workspace, data)

    result = apply_mutation(cfg_path, _mutate_delete_project, {"name": "only"})

    assert result["ok"] is True
    assert "only" not in result["config"].get("projects", {})


# ---------------------------------------------------------------------------
# Test 10: update settings
# ---------------------------------------------------------------------------

def test_update_settings(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    result = apply_mutation(cfg_path, _mutate_settings, {
        "max_parallel": 4, "timeout": 1200,
    })

    assert result["ok"] is True
    assert result["config"]["settings"]["max_parallel"] == 4
    assert result["config"]["settings"]["timeout"] == 1200


# ---------------------------------------------------------------------------
# Test 11: invalid mutation rejected by load_config
# ---------------------------------------------------------------------------

def test_invalid_mutation_rejected(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)

    # Set invalid tool value — load_config should reject
    result = apply_mutation(cfg_path, _mutate_upsert_task, {
        "project": "projA",
        "id": "bad",
        "prompt": "Bad task",
        "tool": "invalid_tool",
    })

    assert result["ok"] is False
    assert any("tool must be one of" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Test 12: update existing project path
# ---------------------------------------------------------------------------

def test_update_project_path(tmp_workspace):
    data = _base_config(tmp_workspace)
    cfg_path = _write_config(tmp_workspace, data)
    new_path = os.path.join(tmp_workspace, "projB")

    result = apply_mutation(cfg_path, _mutate_update_project, {
        "name": "projA", "path": new_path,
    })

    assert result["ok"] is True
    assert result["config"]["projects"]["projA"]["path"] == new_path
