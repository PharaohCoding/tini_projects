from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
import re

from backend.models import AgentResult, ToolCall

DEMO_HINT = re.compile(r"并行|调度|demo", re.IGNORECASE)
FAIL_TOKEN = "[fail]"


class AgentAdapter(ABC):
    """Thin LLM boundary. Coordinator / Worker services must not import model SDKs."""

    @abstractmethod
    def complete(self, messages: list[dict[str, Any]], tools: list[str], **kwargs: Any) -> AgentResult:
        raise NotImplementedError


class MockAgentAdapter(AgentAdapter):
    """Deterministic canned replies. No network, no API keys."""

    def complete(self, messages: list[dict[str, Any]], tools: list[str], **kwargs: Any) -> AgentResult:
        phase = kwargs.get("phase") or "user"
        if phase == "summary":
            return self._summary(kwargs)
        user_text = str(kwargs.get("user_text") or "")
        is_first_user = bool(kwargs.get("is_first_user"))
        reports = kwargs.get("worker_reports") or []
        return self._user_turn(user_text, is_first_user=is_first_user, reports=reports)

    def _user_turn(self, user_text: str, *, is_first_user: bool, reports: list[dict[str, Any]]) -> AgentResult:
        text = user_text.strip()
        if "/demo-fairness" in text:
            return AgentResult(
                text="已按公平性脚本入队：3×research + 1×test。调度器放行后应出现双 research 占槽，队头再遇 research 时让路给 test。",
                tool_calls=[
                    self._spawn("研究 A", "research", "公平性对照：research A"),
                    self._spawn("研究 B", "research", "公平性对照：research B"),
                    self._spawn("研究 C", "research", "公平性对照：research C（预期排队）"),
                    self._spawn("测试", "test", "公平性对照：test（队头同 kind 时应被放行）"),
                    ToolCall(name="finish", args={}),
                ],
            )
        if "/demo-schedule" in text or (is_first_user and DEMO_HINT.search(text)):
            return AgentResult(
                text="已入队 4 个 Worker（research / implement / test / generic）。谁先跑由 Scheduler 决定，我只负责入队。",
                tool_calls=[
                    self._spawn("研究", "research", "demo-schedule：调研现状并写报告"),
                    self._spawn("实现", "implement", "demo-schedule：在沙箱落地最小实现"),
                    self._spawn("测试", "test", "demo-schedule：编写并复述测试结果"),
                    self._spawn("通用", "generic", "demo-schedule：整理通用笔记"),
                    ToolCall(name="finish", args={}),
                ],
            )
        if FAIL_TOKEN in text:
            kind = "research"
            if re.search(r"测试", text):
                kind = "test"
            elif re.search(r"实现|开发", text):
                kind = "implement"
            title = {"research": "研究（失败演练）", "test": "测试（失败演练）", "implement": "实现（失败演练）"}.get(
                kind, "通用（失败演练）"
            )
            return AgentResult(
                text="已入队 1 个会失败的 Mock Worker，用于验证 failed 路径。",
                tool_calls=[
                    self._spawn(title, kind, f"mock fail path {FAIL_TOKEN} {text}"),
                    ToolCall(name="finish", args={}),
                ],
            )
        if re.search(r"研究", text):
            return AgentResult(
                text="已入队 1 个 research Worker。",
                tool_calls=[
                    self._spawn("研究", "research", text),
                    ToolCall(name="finish", args={}),
                ],
            )
        if re.search(r"测试", text):
            return AgentResult(
                text="已入队 1 个 test Worker。",
                tool_calls=[
                    self._spawn("测试", "test", text),
                    ToolCall(name="finish", args={}),
                ],
            )
        if re.search(r"实现|开发", text):
            return AgentResult(
                text="已入队 1 个 implement Worker（只写沙箱，不改外部仓库）。",
                tool_calls=[
                    self._spawn("实现", "implement", text),
                    ToolCall(name="finish", args={}),
                ],
            )
        note_line = text.replace("\n", " ").strip()[:200] or "(empty)"
        return AgentResult(
            text="已记下。需要并行调度演示请发送 /demo-schedule。",
            tool_calls=[
                ToolCall(
                    name="write_context",
                    args={
                        "path": "notes.md",
                        "content": None,
                        "append_line": f"- 用户：{note_line}",
                    },
                ),
                ToolCall(name="finish", args={}),
            ],
        )

    def _summary(self, kwargs: dict[str, Any]) -> AgentResult:
        reports: list[dict[str, Any]] = kwargs.get("worker_reports") or []
        notes = str(kwargs.get("notes") or "")
        lines = ["## 自动汇总", ""]
        if not reports:
            lines.append("- 当前没有可汇总的 Worker 报告。")
        for item in reports:
            status = item.get("status") or "done"
            kind = item.get("kind") or "generic"
            title = item.get("title") or item.get("id")
            summary = item.get("result_summary") or "已完成 Mock 任务。"
            lines.append(f"- [{status}] {title} ({kind}): {summary}")
        lines.append("")
        lines.append("冲突项：无（Mock 不选边）。")
        new_notes = _upsert_summary_section(notes, "\n".join(lines) + "\n")
        done_n = sum(1 for r in reports if r.get("status") == "done")
        failed_n = sum(1 for r in reports if r.get("status") == "failed")
        reply = f"汇总：{done_n} 个完成"
        if failed_n:
            reply += f"，{failed_n} 个失败"
        reply += "。已更新 notes.md。下一步：查看调度条或继续对话。"
        return AgentResult(
            text=reply,
            tool_calls=[
                ToolCall(name="write_context", args={"path": "notes.md", "content": new_notes}),
                ToolCall(name="finish", args={}),
            ],
        )

    def _spawn(self, title: str, kind: str, assignment: str, priority: str = "normal") -> ToolCall:
        return ToolCall(
            name="spawn_worker",
            args={
                "title": title,
                "kind": kind,
                "assignment": assignment,
                "priority": priority,
                "environment": "cloud",
            },
        )


def _upsert_summary_section(notes: str, section: str) -> str:
    marker = "## 自动汇总"
    if not notes.strip():
        return "# notes.md\n\n" + section
    if marker in notes:
        prefix = notes.split(marker, 1)[0].rstrip()
        return prefix + "\n\n" + section
    return notes.rstrip() + "\n\n" + section
