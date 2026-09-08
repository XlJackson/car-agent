"""s04：事件注册表与默认回调。回调按注册顺序执行。"""
from .permissions import check_permission


class HookRegistry:
    """每个注册表独立，便于测试或给不同 Agent 配置不同扩展。"""

    def __init__(self):
        self.hooks = {
            "UserPromptSubmit": [],
            "PreToolUse": [],
            "PostToolUse": [],
            "Stop": [],
        }

    def register_hook(self, event: str, callback):
        self.hooks[event].append(callback)

    def trigger_hooks(self, event: str, *args):
        for callback in self.hooks[event]:
            try:
                result = callback(*args)
            except Exception as exc:
                # 执行前检查异常：阻止工具。其他事件异常：报告并继续，
                # 避免工具已执行却因为日志失败被误报为执行失败。
                print(f"[Hook] {event} 回调异常：{type(exc).__name__}")
                if event == "PreToolUse":
                    return "执行前检查异常，本次工具未执行。"
                continue
            # 只有这两个事件的返回值参与控制流，不能用 if result：
            # 空字符串等非 None 返回值也应触发控制。
            if event in {"PreToolUse", "Stop"} and result is not None:
                return str(result)
        return None


def permission_hook(tool_name: str, args: dict) -> str | None:
    if not check_permission(tool_name, args):
        return "权限拒绝：本次工具未执行，请勿通过其他工具或命令绕过。"
    return None


def large_output_hook(tool_name: str, args: dict, result: dict):
    if len(result["content"]) > 10000:
        print(f"[Hook] {tool_name} 返回较长内容，终端已省略正文。")


def summary_hook(messages: list) -> None:
    """messages 是本次任务的消息切片，不累计之前对话的工具数量。"""
    count = sum(
        1 for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"[Hook] 本次任务结束，共处理 {count} 个工具请求（含拒绝或失败）。")


def create_default_hooks() -> HookRegistry:
    registry = HookRegistry()
    registry.register_hook("PreToolUse", permission_hook)
    registry.register_hook("PostToolUse", large_output_hook)
    registry.register_hook("Stop", summary_hook)
    # UserPromptSubmit 暂不挂默认回调；需要输入日志或上下文扩展时再注册。
    return registry
