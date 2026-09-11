"""终端只展示工具目标摘要，不展开写入内容或工具结果正文。"""


def brief(value, limit=180):
    text = " ".join(str(value).split())
    text = "".join(char for char in text if char.isprintable())
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def tool_target(name, args):
    if name == "schedule_cron":
        return brief(f"{args.get('cron', '')} | {args.get('prompt', '')}")
    if name == "list_crons":
        return "查看定时任务"
    if name == "cancel_cron":
        return brief(args.get("job_id", "缺少任务 ID"))
    if name == "compact":
        return "本轮工具完成后归档并总结历史"
    if name == "load_skill":
        return brief(args.get("name", "缺少技能名"))
    if name in {"read_file", "write_file", "edit_file"}:
        target = brief(args.get("path", "缺少路径"))
        if name == "read_file" and "limit" in args:
            target += f" | 行数上限：{brief(args['limit'], 12)}"
        return target
    if name == "bash":
        return ("[后台] " if args.get("run_in_background") is True else "") + brief(args.get("command", "缺少命令"))
    if name == "glob":
        return brief(args.get("pattern", "缺少模式"))
    if name == "task":
        return brief(args.get("prompt", "缺少子任务"))
    if name == "todo_write":
        return "更新任务计划"
    return ""


def print_assistant_text(content):
    """展示父 Agent 每轮公开的 text；不展示 thinking 或工具结果。"""
    for block in content:
        if getattr(block, "type", None) == "text" and block.text.strip():
            print(f"\nAgent：{block.text}", flush=True)
