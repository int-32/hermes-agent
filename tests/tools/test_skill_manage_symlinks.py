"""Linked skills must be writable in place without replacing their profile links."""

import json

import pytest


@pytest.fixture
def linked_skill(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    skills = home / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("skills: {}\n", encoding="utf-8")

    from agent import skill_utils
    from tools import skill_manager_tool

    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills)
    skill_utils._raw_config_cache_clear()
    skill_utils._external_dirs_cache_clear()
    target = tmp_path / "shared" / "linked-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: linked-skill\ndescription: Use when testing linked skill writes.\n"
        "---\n\n# Linked skill\n\nOriginal instructions.\n",
        encoding="utf-8",
    )
    yield skills, target
    skill_utils._raw_config_cache_clear()
    skill_utils._external_dirs_cache_clear()


def _manage(*ops):
    from tools.registry import registry

    return json.loads(registry.dispatch("skill_manage", {"operations": list(ops)}))


@pytest.mark.parametrize("categorized", [False, True])
def test_linked_skill_writes_reach_target(linked_skill, categorized):
    skills, target = linked_skill
    parent = skills / "category" if categorized else skills
    parent.mkdir(exist_ok=True)
    link = parent / "linked-skill"
    link.symlink_to(target, target_is_directory=True)
    name = "category/linked-skill" if categorized else "linked-skill"

    result = _manage(
        {"name": name, "action": "patch", "old_string": "Original instructions.",
         "new_string": "Updated instructions."},
        {"name": name, "action": "write_file", "file_path": "references/check.md",
         "file_content": "Supporting instructions.\n"},
    )

    assert result.get("success"), result
    assert "Updated instructions." in (target / "SKILL.md").read_text(encoding="utf-8")
    assert (target / "references/check.md").read_text(encoding="utf-8") == "Supporting instructions.\n"
    assert link.is_symlink()
    assert link.resolve() == target


def test_linked_skill_failed_batch_restores_target_and_preserves_link(linked_skill):
    skills, target = linked_skill
    link = skills / "linked-skill"
    link.symlink_to(target, target_is_directory=True)
    before = (target / "SKILL.md").read_bytes()

    result = _manage(
        {"name": "linked-skill", "action": "patch", "old_string": "Original instructions.",
         "new_string": "Updated instructions."},
        {"name": "linked-skill", "action": "patch", "old_string": "Absent exact text.",
         "new_string": "Never written."},
    )

    assert not result.get("success"), result
    assert result.get("completed_before_failure") == 1, result
    assert "ROLLBACK FAILED" not in result["error"]
    assert (target / "SKILL.md").read_bytes() == before
    assert link.is_symlink()
    assert link.resolve() == target
    assert not link.with_name("linked-skill.rollback-broken").exists()
