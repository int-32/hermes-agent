"""agent.disabled_toolsets accepts a single tool name and strips only that tool."""

from model_tools import _select_tool_names


def test_disabling_one_tool_keeps_the_rest_of_its_toolset():
    tools = _select_tool_names(["skills"], ["skill_manage"], quiet_mode=True)
    assert "skill_manage" not in tools
    assert {"skill_view", "skills_list"} <= tools


def test_disabling_the_whole_toolset_still_strips_every_tool():
    tools = _select_tool_names(["skills", "file"], ["skills"], quiet_mode=True)
    assert not tools & {"skill_manage", "skill_view", "skills_list"}
    assert tools


def test_single_tool_name_is_not_accepted_for_enabling():
    assert _select_tool_names(["skill_manage"], None, quiet_mode=True) == set()
