from __future__ import annotations

TEAM_LEADER_TOOLSET_SCOPE = "exact"
TEAM_LEADER_CONVERSATION_TOOLSETS = (
    "team_mission_conversation_leader",
    "clarify",
    "vision",
    "file",
    "terminal",
    "todo",
)
TEAM_LEADER_DISABLED_TOOLSETS = ("delegation",)
TEAM_LEADER_BLOCKED_TOOLS = ("delegate_task",)
TEAM_LEADER_DIRECT_REPLY_REASONING_CONFIG = {"enabled": False}
TEAM_LEADER_DIRECT_REPLY_NO_START_MARKERS = (
    "不要启动团队任务",
    "不要发起团队任务",
    "不要创建团队任务",
    "不要启动任务",
    "不要发起任务",
    "不要创建任务",
    "不需要团队任务",
    "无需团队任务",
    "别启动团队任务",
    "别发起团队任务",
    "do not start a team mission",
    "don't start a team mission",
    "do not start team mission",
    "don't start team mission",
    "do not launch a team mission",
    "don't launch a team mission",
    "do not start a team task",
    "don't start a team task",
)
TEAM_LEADER_DIRECT_REPLY_SELF_MARKERS = (
    "自己完成",
    "你自己完成",
    "你来完成",
    "leader自己完成",
    "leader 直接完成",
    "answer directly",
    "reply directly",
)
TEAM_LEADER_DIRECT_REPLY_NEGATED_SELF_MARKERS = (
    "不要直接自己完成",
    "不要自己完成",
    "别自己完成",
    "不要你自己完成",
    "别你自己完成",
    "不要你来完成",
    "别你来完成",
    "do not answer directly",
    "don't answer directly",
    "do not reply directly",
    "don't reply directly",
)
TEAM_LEADER_START_TASK_MARKERS = (
    "启动团队任务",
    "发起团队任务",
    "创建团队任务",
    "执行团队任务",
    "开始团队任务",
    "启动一个团队任务",
    "发起一个团队任务",
    "创建一个团队任务",
    "执行一个团队任务",
    "让团队",
    "团队来",
    "团队执行",
    "团队协作",
    "任务图",
    "成员节点",
    "汇总节点",
    "start a team mission",
    "launch a team mission",
    "create a team mission",
    "run a team mission",
    "start a team task",
    "launch a team task",
    "create a team task",
    "run a team task",
)
