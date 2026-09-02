import os
import subprocess

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


# ============================================================
# 2. System Prompt
# ============================================================

SYSTEM_PROMPT = f"""
你是一个运行在以下工作目录中的编程智能体：

{os.getcwd()}

你的任务是根据用户的要求，使用提供的工具完成实际操作。

当任务需要读取文件、查看目录、运行程序或执行其他系统操作时，
应主动调用可用工具，而不是只告诉用户应该执行什么命令。

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
    }
]


# ============================================================
# 4. Tool Executor
# ============================================================

def run_bash(command: str) -> str:
    """
    执行 Shell 命令，并返回标准输出和标准错误。
    """

    dangerous_commands = [
        "rm -rf /",
        "shutdown",
        "reboot",
        "sudo",
    ]

    if any(item in command for item in dangerous_commands):
        return "错误：检测到危险命令，已阻止执行。"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
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
# 5. Agent Loop
# ============================================================

def agent_loop(messages: list):
    while True:

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

        # 如果模型没有调用工具，
        # 说明它认为当前任务已经完成
        if not tool_calls:
            return

        # ---------- 执行工具 ----------
        tool_results = []

        for tool_call in tool_calls:

            if tool_call.name == "bash":
                command = tool_call.input["command"]

                print(f"\n执行命令：$ {command}")

                output = run_bash(command)

                print(output)

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": output,
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
# 6. CLI
# ============================================================

def main():

    print("Car Agent - s01 Agent Loop")
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
        print(f"\nAgent 最终回答：{last_message}")
        content = last_message["content"]

        if isinstance(content, list):
            for block in content:
                if getattr(block, "type", None) == "text":
                    print(f"\nAgent：{block.text}")

        print()


if __name__ == "__main__":
    main()