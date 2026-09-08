"""Agent 主体：模型调用、工具执行与消息回填；扩展行为挂在 Hooks 上。"""
import os
import sys
from pathlib import Path

# 同时支持 python src/car_agent/agent_loop.py 与包入口启动。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anthropic import Anthropic
from car_agent import config
from car_agent.hooks import create_default_hooks
from car_agent.permissions import FILE_TOOLS
from car_agent.tools import TOOLS, TOOL_HANDLERS

client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
)
HOOKS = create_default_hooks()


def print_tool_calls(tool_calls: list, round_number: int) -> None:
    names = "、".join(call.name for call in tool_calls) or "无"
    print(f"\n第 {round_number} 轮 | {len(tool_calls)} 个工具调用：{names}")


def execute_tool(tool_call, hooks) -> dict:
    """每个请求对应一个结果；拒绝时不执行工具，也不触发 PostToolUse。"""
    result = {
        "type": "tool_result",
        "tool_use_id": tool_call.id,
        "content": "",
        "is_error": False,
    }
    try:
        handler = TOOL_HANDLERS.get(tool_call.name)
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


def agent_loop(messages: list, *, hooks=None, max_rounds: int = config.MAX_ROUNDS) -> bool:
    """True 表示正常结束；False 表示达到轮数上限。"""
    if max_rounds < 1:
        raise ValueError("max_rounds 必须大于 0。")
    hooks = HOOKS if hooks is None else hooks
    task_start = len(messages)
    for round_number in range(1, max_rounds + 1):
        response = client.messages.create(
            model=config.MODEL,
            system=config.SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
            max_tokens=4096,
        )
        messages.append({"role": "assistant", "content": response.content})
        tool_calls = [block for block in response.content if block.type == "tool_use"]
        print_tool_calls(tool_calls, round_number)

        if not tool_calls:
            force = hooks.trigger_hooks("Stop", messages[task_start:])
            if force is None:
                return True
            # 非 None 表示请求继续；空字符串也遵守此约定。
            messages.append({"role": "user", "content": force or "请继续检查本次任务。"})
            continue

        tool_results = [execute_tool(call, hooks) for call in tool_calls]
        messages.append({"role": "user", "content": tool_results})

    # 硬上限不经过可要求续跑的 Stop 事件，避免 Hook 绕过上限。
    # 本轮工具结果已全部回填，下次用户提问仍有完整消息历史。
    print(f"\n已达到 {max_rounds} 轮上限，停止本次任务；任务可能尚未完成。")
    return False


def submit_query(query: str, history: list, *, hooks=None) -> bool:
    hooks = HOOKS if hooks is None else hooks
    # 回调可以原地修改 history 来加入上下文；返回值不控制流程。
    hooks.trigger_hooks("UserPromptSubmit", query, history)
    history.append({"role": "user", "content": query})
    return agent_loop(history, hooks=hooks)


def main():
    print("Car Agent - s04 Hooks")
    print(f"输入 q / exit 退出；每次任务最多 {config.MAX_ROUNDS} 轮模型调用\n")
    history = []
    while True:
        try:
            query = input("car-agent >> ").strip()
            if query.lower() in {"q", "exit"}:
                break
            if not query:
                continue
            completed = submit_query(query, history)
            if completed:
                for block in history[-1]["content"]:
                    if getattr(block, "type", None) == "text":
                        print(f"\nAgent：{block.text}")
            print()
        except (KeyboardInterrupt, EOFError):
            # 直接退出，不复用可能缺少 tool_result 的中断历史。
            print("\n已退出 Agent。")
            break


if __name__ == "__main__":
    main()
