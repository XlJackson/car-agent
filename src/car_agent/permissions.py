"""s03：权限决策，由 PreToolUse Hook 调用。"""
import re
from . import config
from .config import resolve_path


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
        "check": lambda args: not resolve_path(args["path"]).is_relative_to(config.WORKDIR),
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
