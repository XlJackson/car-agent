"""Agent 主体：模型调用、工具执行与消息回填；扩展行为挂在 Hooks 上。"""
import os
import sys
from functools import partial
from pathlib import Path

# 同时支持 python src/car_agent/agent_loop.py 与包入口启动。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anthropic import Anthropic
from anthropic.types import TextBlock
from car_agent import config
from car_agent.hooks import create_default_hooks
from car_agent.permissions import FILE_TOOLS
from car_agent.tools import TOOLS, TOOL_HANDLERS
from car_agent.todo import TodoManager
from car_agent.subagent import TASK_TOOL, run_subagent
from car_agent.display import brief, tool_target, print_assistant_text
from car_agent.skill_loader import build_system_prompt, SKILL_LOADER
from car_agent.compact import ContextCompactor, COMPACT_TOOL, context_too_long
from car_agent.memory import MemoryManager, memory_system

client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
)
HOOKS = create_default_hooks()


def clean_response_content(content: list) -> list:
    """过滤无法回传 API 的空文本块，保留工具调用及其他类型块。"""
    cleaned = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
        if kind == "text" and not text.strip():
            continue
        cleaned.append(block)
    # 避免把空 content 数组存入后续对话历史，不虚构模型完成了任务。
    return cleaned or [TextBlock(type="text", text="模型本轮未返回有效文本或工具调用。")]


def print_tool_calls(tool_calls: list, round_number: int, prefix: str = "") -> None:
    names = "、".join(call.name for call in tool_calls) or "无"
    print(f"\n{prefix}第 {round_number} 轮 | {len(tool_calls)} 个工具调用：{names}")


def execute_tool(tool_call, hooks, *, handlers=None) -> dict:
    """每个请求对应一个结果；拒绝时不执行工具，也不触发 PostToolUse。"""
    result = {
        "type": "tool_result",
        "tool_use_id": tool_call.id,
        "content": "",
        "is_error": False,
    }
    try:
        handler = (TOOL_HANDLERS if handlers is None else handlers).get(tool_call.name)
        if handler is None:
            raise ValueError(f"未知工具：{tool_call.name}")
        args = dict(tool_call.input)
        if tool_call.name in FILE_TOOLS:
            args["path"] = str(config.resolve_path(args["path"]))
        blocked = hooks.trigger_hooks("PreToolUse", tool_call.name, args)
        if blocked is not None:
            result.update(content=blocked, is_error=True)
            return result
    except Exception as exc:
        result.update(content=f"错误：{exc}", is_error=True)
        return result

    try:
        result["content"] = handler(**args)
    except Exception as exc:
        result.update(content=f"错误：{exc}", is_error=True)
    # 已尝试执行的工具，无论成功还是异常，均触发执行后事件。
    hooks.trigger_hooks("PostToolUse", tool_call.name, args, result)
    return result


def agent_loop(messages: list, *, hooks=None, max_rounds: int = config.MAX_ROUNDS,
               tool_schemas=None, tool_handlers=None, system_prompt=None,
               is_subagent: bool = False, active_request: str | None = None) -> bool:
    """True 表示正常结束；False 表示达到轮数上限。"""
    if max_rounds < 1:
        raise ValueError("max_rounds 必须大于 0。")
    hooks = HOOKS if hooks is None else hooks
    task_messages = []  # Hook 的本次任务记录不随模型上下文裁剪而丢失。
    if active_request is None:
        active_request = next((m["content"] for m in reversed(messages)
                               if m.get("role") == "user" and isinstance(m.get("content"), str)), "未提供用户请求")
    todo = None if is_subagent else TodoManager()
    schemas = list(TOOLS if tool_schemas is None else tool_schemas)
    handlers = dict(TOOL_HANDLERS if tool_handlers is None else tool_handlers)
    if is_subagent:
        # 不仅不向模型展示，也在执行分发表中移除，防止伪造调用递归。
        schemas = [tool for tool in schemas if tool["name"] not in {"task", "todo_write"}]
        handlers = {name: handler for name, handler in handlers.items() if name not in {"task", "todo_write"}}
    else:
        handlers["todo_write"] = todo.write
        base_tools = [tool for tool in schemas if tool["name"] not in {"task", "todo_write"}]
        base_handlers = {name: handler for name, handler in handlers.items() if name not in {"task", "todo_write"}}
        handlers["task"] = partial(run_subagent, run_loop=agent_loop, hooks=hooks,
                                   tools=base_tools, handlers=base_handlers)
        schemas.append(TASK_TOOL)
    prefix = "[sub] " if is_subagent else ""
    compactor = ContextCompactor(client, active_request, prefix=prefix)
    handlers["compact"] = compactor.request
    schemas = [schema for schema in schemas if schema["name"] != "compact"] + [COMPACT_TOOL]
    active_system = build_system_prompt(config.SYSTEM_PROMPT if system_prompt is None else system_prompt)
    for round_number in range(1, max_rounds + 1):
        compactor.plan = todo.render() if todo is not None else ""
        messages[:] = compactor.prepare(messages)
        for retry in range(2):
            try:
                response = client.messages.create(
                    model=config.MODEL, system=active_system, messages=messages,
                    tools=schemas, max_tokens=4096,
                )
                break
            except Exception as exc:
                if not context_too_long(exc) or retry:
                    raise
                messages[:] = compactor.compact_history(messages, reactive=True)
        compactor.mark_consumed(messages)
        messages.append({"role": "assistant", "content": clean_response_content(response.content)})
        task_messages.append(messages[-1])
        if not is_subagent:
            print_assistant_text(messages[-1]["content"])
        tool_calls = [block for block in response.content if block.type == "tool_use"]
        print_tool_calls(tool_calls, round_number, prefix)

        if not tool_calls:
            force = hooks.trigger_hooks("Stop", task_messages)
            if force is None:
                if todo is not None:
                    todo.report_unfinished()
                return True
            # 非 None 表示请求继续；空字符串也遵守此约定。
            messages.append({"role": "user", "content": force or "请继续检查本次任务。"})
            task_messages.append(messages[-1])
            continue

        tool_results = []
        for call in tool_calls:
            # 执行到该工具时才显示目标，不把排队的调用误报为已执行。
            print(f"  {prefix}→ {call.name}：{tool_target(call.name, call.input)}", flush=True)
            result = execute_tool(call, hooks, handlers=handlers)
            if result["is_error"]:
                print(f"  {prefix}失败/拒绝：{brief(result['content'])}", flush=True)
            tool_results.append(result)
        reminder = todo.after_tool_round(tool_calls, tool_results) if todo is not None else None
        if reminder is not None:
            tool_results.append(reminder)
        messages.append({"role": "user", "content": tool_results})
        task_messages.append(messages[-1])
        if compactor.requested:
            compactor.requested = False
            compactor.plan = todo.render() if todo is not None else ""
            # 延迟到整个工具批次闭合后，避免丢失同批操作及其结果。
            messages[:] = compactor.compact_history(messages)

    # 硬上限不经过可要求续跑的 Stop 事件，避免 Hook 绕过上限。
    # 本轮工具结果已全部回填，下次用户提问仍有完整消息历史。
    print(f"\n{prefix}已达到 {max_rounds} 轮上限，停止本次任务；任务可能尚未完成。")
    if todo is not None:
        todo.report_unfinished()
    return False


def submit_query(query: str, history: list, *, hooks=None) -> bool:
    hooks = HOOKS if hooks is None else hooks
    # 回调可以原地修改 history 来加入上下文；返回值不控制流程。
    hooks.trigger_hooks("UserPromptSubmit", query, history)
    history.append({"role": "user", "content": query})
    memory = MemoryManager(client) if config.MEMORY_ENABLED else None
    recalled = ""
    if memory is not None:
        try:
            recalled = memory.recall(query)
        except Exception:
            print("[Memory] 召回失败，本轮继续使用当前对话。")
    completed = agent_loop(history, hooks=hooks, active_request=query,
                           system_prompt=memory_system(config.SYSTEM_PROMPT, recalled))
    if completed and memory is not None:
        try:
            memory.extract(query)
        except Exception:
            print("[Memory] 记忆提取或整理失败，回答不受影响；已成功写入的记忆仍保留。")
    return completed


def main():
    print("Car Agent - s09 Memory")
    print(f"长期记忆：{'开启' if config.MEMORY_ENABLED else '关闭'}（项目 .memory/）")
    print(f"可用技能：{', '.join(SKILL_LOADER.skills) or '无'}（启动时扫描 skills/）")
    print(f"输入 q / exit 退出；每次任务最多 {config.MAX_ROUNDS} 轮模型调用\n")
    history = []
    while True:
        try:
            query = input("car-agent >> ").strip()
            if query.lower() in {"q", "exit"}:
                break
            if not query:
                continue
            submit_query(query, history)
            print()
        except (KeyboardInterrupt, EOFError):
            # 直接退出，不复用可能缺少 tool_result 的中断历史。
            print("\n已退出 Agent。")
            break


if __name__ == "__main__":
    main()
