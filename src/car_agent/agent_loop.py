import os
import re
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv


# ============================================================
# 1. 加载环境变量
# ============================================================

load_dotenv()

client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    base_url=os.getenv("ANTHROPIC_BASE_URL"),
)

MODEL = os.getenv("MODEL_ID")
WORKDIR = Path.cwd().resolve()


# ============================================================
# 2. System Prompt
# ============================================================

SYSTEM_PROMPT = f"""
你是一个运行在以下工作目录中的编程智能体：

{WORKDIR}

你的任务是根据用户的要求，使用提供的工具完成实际操作。

当任务需要读取文件、查看目录、运行程序或执行其他系统操作时，
应主动调用可用工具，而不是只告诉用户应该执行什么命令。
读、写、修改和查找文件时，优先使用对应的专用工具。
工具被权限检查拒绝后，不要换工具或命令绕过拒绝，应向用户说明情况。

当你已经获得足够的信息并完成任务后，直接向用户返回最终结果。
"""


# ============================================================
# 3. Tool Schema
# ============================================================

TOOLS = [
    {
        "name": "bash",
        "description": "在当前工作目录中执行一条 Shell 命令。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "需要在终端中执行的 Shell 命令。",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取 UTF-8 文本文件，可限制行数；工作目录外的路径需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "创建或覆盖 UTF-8 文本文件，父目录须存在；工作目录外的路径需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "将文件中第一次出现的 old_text 替换为 new_text。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "minLength": 1},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "按相对路径模式查找工作目录内的文件，例如 **/*.py，最多显示 200 条。",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]


# ============================================================
# 4. Tool Executor
# ============================================================

def run_bash(command: str) -> str:
    """
    执行 Shell 命令，并返回标准输出和标准错误。
    """

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )

        output = (result.stdout + result.stderr).strip()

        if not output:
            return "命令执行成功，但没有输出。"

        return output[:50000]

    except subprocess.TimeoutExpired:
        return "错误：命令执行超时。"

    except Exception as e:
        return f"错误：{e}"


# ============================================================
# 5. 文件工具与分发表
# ============================================================

def resolve_path(path: str) -> Path:
    """统一解析路径；是否允许访问由执行前的权限管线判断。"""
    return (WORKDIR / path).resolve()


def run_read(path: str, limit: int | None = None) -> str:
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit 必须是正整数。")
    with resolve_path(path).open(encoding="utf-8") as file:
        if limit is None:
            return file.read()
        from itertools import islice
        return "".join(islice(file, limit))


def run_write(path: str, content: str) -> str:
    resolve_path(path).write_text(content, encoding="utf-8")
    return f"已写入 {path}，共 {len(content)} 个字符。"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    if not old_text:
        raise ValueError("old_text 不能为空。")
    target = resolve_path(path)
    text = target.read_text(encoding="utf-8")
    if old_text not in text:
        raise ValueError("未找到需要替换的文本。")
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"已修改 {path}。"


def run_glob(pattern: str) -> str:
    if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("请使用不含 .. 的相对路径模式。")
    matches = []
    for path in WORKDIR.glob(pattern):
        target = resolve_path(str(path))
        if not target.is_relative_to(WORKDIR):
            continue
        if target.is_file():
            matches.append(str(path.relative_to(WORKDIR)))
    matches = sorted(set(matches))
    shown = matches[:200]
    if len(matches) > 200:
        shown.append("... 更多结果已省略，请缩小查找范围。")
    return "\n".join(shown) or "未找到匹配文件。"


# Schema 告诉模型如何调用；Handler 告诉程序实际执行什么。
# 增加工具：实现函数 + 添加 Schema + 在此注册；不修改循环。
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# ============================================================
# 6. Permission：执行前的三道闸门
# ============================================================

# 教学用字符串/正则匹配，并非 Shell 沙箱，不能覆盖所有命令变体。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
FILE_TOOLS = {"read_file", "write_file", "edit_file"}
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)


def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"命中硬拒绝规则：{pattern}"
    return None


PERMISSION_RULES = [
    {
        "tools": FILE_TOOLS,
        "check": lambda args: not resolve_path(args["path"]).is_relative_to(WORKDIR),
        "message": "访问工作目录外的文件",
    },
    {
        "tools": {"bash"},
        "check": lambda args: bool(DESTRUCTIVE_COMMAND_WORD.search(args["command"]))
        or any(word in args["command"] for word in ["rm ", "> /etc/", "chmod 777"]),
        "message": "命令可能删除文件或修改系统设置",
    },
]


def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


def ask_user(tool_name: str, args: dict, reason: str) -> bool:
    print(f"\n需要审批：{reason} | 工具：{tool_name}")
    # 不打印文件正文；命令须完整展示，以便知道批准的具体操作。
    target = args["command"] if tool_name == "bash" else args["path"]
    print(f"目标：{target!r}")
    try:
        return input("仅允许本次操作？[y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print("\n未获得确认，拒绝本次操作。")
        return False


def check_permission(tool_name: str, args: dict) -> bool:
    if tool_name == "bash":
        reason = check_deny_list(args["command"])
        if reason:
            print(f"\n已阻止：{reason}")
            return False
    reason = check_rules(tool_name, args)
    if reason:
        return ask_user(tool_name, args, reason)
    return True


# ============================================================
# 7. Agent Loop
# ============================================================

def print_tool_calls(tool_calls: list, round_number: int) -> None:
    """每轮只展示工具数量和名称，避免大段参数刷屏。"""
    names = "、".join(tool_call.name for tool_call in tool_calls) or "无"
    print(f"\n第 {round_number} 轮 | {len(tool_calls)} 个工具调用：{names}")


def agent_loop(messages: list):
    round_number = 0  # 每次用户提问重新计数
    while True:
        round_number += 1

        # ---------- 调用 LLM ----------
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
            max_tokens=4096,
        )

        # 保存模型这一轮的输出
        messages.append(
            {
                "role": "assistant",
                "content": response.content,
            }
        )

        # ---------- 查找 tool_use ----------
        tool_calls = [
            block
            for block in response.content
            if block.type == "tool_use"
        ]
        print_tool_calls(tool_calls, round_number)

        # 如果模型没有调用工具，
        # 说明它认为当前任务已经完成
        if not tool_calls:
            return

        # ---------- 执行工具 ----------
        tool_results = []

        for tool_call in tool_calls:

            is_error = False
            try:
                handler = TOOL_HANDLERS.get(tool_call.name)
                if handler is None:
                    raise ValueError(f"未知工具：{tool_call.name}")
                args = dict(tool_call.input)
                if tool_call.name in FILE_TOOLS:
                    # 审批和执行使用同一个解析后的目标，不修改历史消息。
                    args["path"] = str(resolve_path(args["path"]))
                if check_permission(tool_call.name, args):
                    output = handler(**args)
                else:
                    output = "权限拒绝：本次工具未执行，请勿通过其他工具或命令绕过。"
                    is_error = True
            except Exception as e:
                # 一个工具失败也要回传结果，让模型有机会纠正参数。
                output = f"错误：{e}"
                is_error = True

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": output,
                    "is_error": is_error,
                }
            )

        # ---------- 将工具执行结果反馈给模型 ----------
        messages.append(
            {
                "role": "user",
                "content": tool_results,
            }
        )


# ============================================================
# 8. CLI
# ============================================================

def main():

    print("Car Agent - s03 Permission")
    print("输入 q / exit 退出\n")

    history = []

    while True:

        query = input("car-agent >> ").strip()

        if query.lower() in ("q", "exit"):
            break

        if not query:
            continue

        # 保存用户消息
        history.append(
            {
                "role": "user",
                "content": query,
            }
        )

        # 启动 Agent Loop
        agent_loop(history)

        # Agent Loop 结束后，
        # 最后一条消息通常是 assistant 的最终回答
        last_message = history[-1]
        content = last_message["content"]

        if isinstance(content, list):
            for block in content:
                if getattr(block, "type", None) == "text":
                    print(f"\nAgent：{block.text}")

        print()


if __name__ == "__main__":
    main()
