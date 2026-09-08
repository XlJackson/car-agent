"""公共配置和路径解析；导入时不创建模型客户端。"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
MODEL = os.getenv("MODEL_ID")
WORKDIR = Path.cwd().resolve()
MAX_ROUNDS = 15


def resolve_path(path: str) -> Path:
    """解析路径，访问权限由 PreToolUse Hook 判断。"""
    return (WORKDIR / path).resolve()


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
