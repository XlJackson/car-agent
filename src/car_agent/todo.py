"""s05：本次任务的计划状态、终端展示和按工具轮次计数的提醒。"""
import ast
import json


class TodoManager:
    def __init__(self):
        self.items = []
        self.rounds_since_todo = 0

    def update(self, todos: list | str) -> str:
        if isinstance(todos, str):
            if len(todos) > 20000:
                raise ValueError("计划输入过长。")
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (ValueError, SyntaxError) as exc:
                    raise ValueError("todos 必须是列表、JSON 或 Python 列表表示。") from exc
        if not isinstance(todos, list) or len(todos) > 20:
            raise ValueError("todos 必须是列表，最多 20 项。")
        validated = []
        for item in todos:
            if not isinstance(item, dict):
                raise ValueError("每个步骤必须是包含 content 和 status 的对象。")
            content, status = item.get("content"), item.get("status")
            if not isinstance(content, str) or not content.strip() or len(content) > 200:
                raise ValueError("步骤 content 必须为 1–200 字符的非空文本。")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError("status 必须是 pending、in_progress 或 completed。")
            validated.append({"content": content.strip(), "status": status})
        if sum(item["status"] == "in_progress" for item in validated) > 1:
            raise ValueError("同一时间最多只能有一个进行中的步骤。")
        # 全部校验通过后再替换，失败时保留上一版计划。
        self.items = validated
        return self.render()

    def render(self) -> str:
        completed = sum(item["status"] == "completed" for item in self.items)
        lines = [f"任务计划（已完成 {completed}/{len(self.items)}）"]
        labels = {"pending": "[ ] 待开始", "in_progress": "[>] 进行中", "completed": "[x] 已完成"}
        for index, item in enumerate(self.items, 1):
            # 每步单行；去掉终端控制字符，正文仍保存在原始计划里。
            content = " ".join(item["content"].split())
            content = "".join(c for c in content if c.isprintable())
            lines.append(f"  {index}. {labels[item['status']]}  {content}")
        if not self.items:
            lines.append("  暂无步骤。")
        return "\n".join(lines)

    def write(self, todos: list | str) -> str:
        previous = [dict(item) for item in self.items]
        output = self.update(todos)
        old_status = {item["content"]: item["status"] for item in previous}
        skipped = [index for index, item in enumerate(self.items, 1)
                   if item["status"] == "completed" and old_status.get(item["content"]) == "pending"]
        if skipped:
            warning = f"步骤 {', '.join(map(str, skipped))} 从待开始直接标为完成，未记录进行中状态。"
            print(f"[计划提示] {warning}")
            output += f"\n计划提示：{warning} 后续请开始前更新状态；不要为补日志重新执行已完成操作。"
        if self.items != previous:
            print(f"\n{self.render()}", flush=True)
        return output

    def after_tool_round(self, calls: list, results: list) -> dict | None:
        updated = any(
            call.name == "todo_write" and not result["is_error"]
            for call, result in zip(calls, results)
        )
        self.rounds_since_todo = 0 if updated else self.rounds_since_todo + 1
        if self.rounds_since_todo < 3:
            return None
        self.rounds_since_todo = 0
        return {
            "type": "text",
            "text": "<reminder>请使用 todo_write 更新本次任务的完整计划。"
            "仅将已实际完成并检查的步骤标为 completed；受阻时说明原因。\n"
            + self.render() + "</reminder>",
        }

    def report_unfinished(self):
        remaining = sum(item["status"] != "completed" for item in self.items)
        if remaining:
            print(f"[计划] 仍有 {remaining} 项未标记完成；请结合执行结果和最终答复确认。")


# 默认分发表入口；主循环为每个用户任务绑定独立的 TodoManager。
TODO = TodoManager()


def run_todo_write(todos: list | str) -> str:
    return TODO.write(todos)
