"""Subagent display identity helpers for delegate_task."""

import re
from typing import Any, Dict, List, Optional


SUBAGENT_NAME_MAX_CHARS = 24
SUBAGENT_NAME_DESCRIPTION = (
    "Short human-readable display name for this subagent. Use a concise role "
    "label, not the whole task. Examples: '目录巡检员', '时间校准员', "
    "'计算核对员', '代码诊断员'."
)

_SUBAGENT_NAME_KEYWORDS = (
    (
        ("pwd", "current working directory", "working directory", "工作目录", "当前目录", "目录", "路径"),
        "目录巡检员",
    ),
    (("date", "time", "clock", "时间", "日期", "时区"), "时间校准员"),
    (
        ("python", "calculate", "calculation", "math", "乘", "计算", "算术", "数学"),
        "计算核对员",
    ),
    (
        ("bug", "test", "tests", "code", "代码", "接口", "修复", "测试", "报错", "异常"),
        "代码诊断员",
    ),
    (("data", "dataset", "spreadsheet", "数据", "统计", "分析", "表格"), "数据分析员"),
    (("write", "copy", "content", "文案", "写作", "创作", "发布稿"), "文案撰写员"),
    (("search", "research", "web", "检索", "搜索", "调研", "资料"), "资料检索员"),
    (("file", "read", "write_file", "文件", "读取", "写入"), "文件整理员"),
)
_SUBAGENT_NAME_STOPWORDS = {
    "示例任务",
    "执行示例",
    "请使用",
    "并返回",
    "不要创建",
    "不要修改",
    "当前系统",
}


def clean_subagent_name(value: Optional[Any]) -> str:
    """Normalize caller/model-provided subagent display names."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" '\"`“”‘’[](){}<>《》：:，,。.;；、|-")
    if not text:
        return ""
    if len(text) > SUBAGENT_NAME_MAX_CHARS:
        text = text[:SUBAGENT_NAME_MAX_CHARS].rstrip()
    return text


def humanize_subagent_name(
    goal: Optional[str],
    context: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    task_index: int = 0,
) -> str:
    """Derive a stable, human-readable fallback name from task intent."""
    haystack = f"{goal or ''}\n{context or ''}".lower()
    for keywords, label in _SUBAGENT_NAME_KEYWORDS:
        if any(keyword.lower() in haystack for keyword in keywords):
            return label

    normalized_toolsets = {str(item).strip().lower() for item in (toolsets or [])}
    if "web" in normalized_toolsets:
        return "资料检索员"
    if "browser" in normalized_toolsets:
        return "网页操作员"
    if "terminal" in normalized_toolsets:
        return "终端执行员"
    if normalized_toolsets.intersection({"file", "files"}):
        return "文件整理员"

    cleaned_goal = re.sub(r"`[^`]*`", " ", str(goal or ""))
    cleaned_goal = re.sub(r"https?://\S+|/[^\s]+", " ", cleaned_goal)
    cleaned_goal = re.sub(r"[A-Za-z0-9_./:=+\-*&%$#@!?\"'`]+", " ", cleaned_goal)
    for candidate in re.findall(r"[\u4e00-\u9fff]{2,8}", cleaned_goal):
        if candidate in _SUBAGENT_NAME_STOPWORDS:
            continue
        if any(stop in candidate for stop in _SUBAGENT_NAME_STOPWORDS):
            continue
        suffix = "" if candidate.endswith(("员", "师", "助手")) else "助手"
        return clean_subagent_name(f"{candidate[:6]}{suffix}")

    return f"任务助手 {task_index + 1}"


def resolve_task_agent_name(
    task: Dict[str, Any],
    *,
    fallback_goal: Optional[str],
    fallback_context: Optional[str],
    fallback_toolsets: Optional[List[str]],
    task_index: int,
) -> str:
    explicit = clean_subagent_name(
        task.get("name")
        or task.get("agent_name")
        or task.get("agentName")
        or task.get("display_name")
        or task.get("displayName")
        or task.get("title")
    )
    if explicit:
        return explicit
    return humanize_subagent_name(
        task.get("goal") or fallback_goal,
        task.get("context") or fallback_context,
        task.get("toolsets") or fallback_toolsets,
        task_index,
    )
