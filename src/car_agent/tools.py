"""s02：工具描述、实现和分发表。"""
import subprocess
from pathlib import Path
from . import config
from .config import resolve_path
from .todo import run_todo_write
from .skill_loader import run_load_skill


TOOLS = [
    {
        "name": "load_skill",
        "description": "按技能目录中的名称读取完整 SKILL.md。参数为技能名，不是路径。",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "todo_write",
        "description": "创建或更新本次任务的可见计划。每次提交完整列表（包括已完成步骤）；"
        "执行前标为 in_progress，确认完成后标为 completed。只管理计划，不执行实际操作。",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "minLength": 1, "maxLength": 200},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
    },
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
            cwd=config.WORKDIR,
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
    for path in config.WORKDIR.glob(pattern):
        target = resolve_path(str(path))
        if not target.is_relative_to(config.WORKDIR):
            continue
        if target.is_file():
            matches.append(str(path.relative_to(config.WORKDIR)))
    matches = sorted(set(matches))
    shown = matches[:200]
    if len(matches) > 200:
        shown.append("... 更多结果已省略，请缩小查找范围。")
    return "\n".join(shown) or "未找到匹配文件。"


# Schema 告诉模型如何调用；Handler 告诉程序实际执行什么。
# 增加工具：实现函数 + 添加 Schema + 在此注册；不修改循环。
TOOL_HANDLERS = {
    "load_skill": run_load_skill,
    "todo_write": run_todo_write,
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}
