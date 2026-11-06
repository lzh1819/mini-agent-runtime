"""验证技能目录、按需进入对话和工具反馈，不请求外部 API。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
import yaml
from openai.types.chat import ChatCompletion

from experiments import s07_skill_loading as s07


SKILL = """---
name: review
description: >-
  Review code
  for actual bugs.
---

# Review instructions

Read the callers before reporting a bug.
"""


def test_catalog_contains_metadata_but_not_instructions(tmp_path, monkeypatch):
    manifest = tmp_path / "review-folder" / "SKILL.md"
    manifest.parent.mkdir()
    manifest.write_text(SKILL)
    plain = tmp_path / "plain" / "SKILL.md"
    plain.parent.mkdir()
    plain.write_text("# Plain skill\n\nDetailed plain instructions.")
    loader = s07.SkillLoader(tmp_path)
    monkeypatch.setattr(s07, "SKILL_LOADER", loader)

    assert loader.catalog() == (
        "- plain: Plain skill\n- review: Review code for actual bugs."
    )
    system = s07.build_system_prompt()
    assert loader.catalog() in system
    assert "Read the callers" not in system
    assert "Detailed plain instructions" not in system
    assert loader.load("review") == SKILL


def test_load_skill_enters_conversation_as_tool_result(tmp_path, monkeypatch):
    manifest = tmp_path / "review" / "SKILL.md"
    manifest.parent.mkdir()
    manifest.write_text(SKILL)
    loader = s07.SkillLoader(tmp_path)
    monkeypatch.setattr(s07, "SKILL_LOADER", loader)
    monkeypatch.setitem(s07.TOOL_HANDLERS, "load_skill", loader.load)
    events = []
    monkeypatch.setattr(s07, "HOOKS", {
        "UserPromptSubmit": [], "Stop": [],
        "PreToolUse": [lambda name, args: events.append(("before", name))],
        "PostToolUse": [lambda name, args, output: events.append(("after", name, output))],
    })
    responses = iter([
        ChatCompletion.model_validate({
            "id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "load", "type": "function", "function": {
                        "name": "load_skill", "arguments": json.dumps({"name": "review"}),
                    },
                }],
            }}],
        }),
        ChatCompletion.model_validate({
            "id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "Review complete.",
            }}],
        }),
    ])
    requests = []

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        return next(responses)

    client = Mock()
    client.chat.completions.create.side_effect = create
    messages = [
        {"role": "system", "content": s07.build_system_prompt()},
        {"role": "user", "content": "Load review and inspect the code."},
    ]
    s07.agent_loop(messages, client, "test")

    assert "Read the callers" not in str(requests[0]["messages"])
    assert requests[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "load", "content": SKILL,
    }
    assert events == [("before", "load_skill"), ("after", "load_skill", SKILL)]
    assert "load_skill" in [tool["function"]["name"] for tool in requests[0]["tools"]]


def test_unknown_skill_name_is_not_used_as_a_path(tmp_path):
    loader = s07.SkillLoader(tmp_path / "missing-skills")
    (tmp_path / "private.md").write_text("Private contents")

    assert loader.catalog() == "(no skills found)"
    assert loader.load("../private.md") == (
        "Error: Unknown skill '../private.md'. Available: none"
    )


def test_scan_excludes_symlinks_outside_skills_root(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(SKILL)
    (skills / "linked-review").symlink_to(outside, target_is_directory=True)

    assert s07.SkillLoader(skills).skills == {}


@pytest.mark.parametrize("content,error", [
    ("---\nname: review\n", ValueError),
    ("---\nname: [\n---\nBody", yaml.YAMLError),
])
def test_malformed_frontmatter_surfaces_errors(content, error):
    with pytest.raises(error):
        s07.SkillLoader.parse_frontmatter(content)
