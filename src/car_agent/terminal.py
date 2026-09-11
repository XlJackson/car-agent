"""交互输入：后台日志显示在输入行上方，保留提示符、文字和光标。"""
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout


class TerminalInput:
    def __init__(self):
        self.session = PromptSession() if sys.stdin.isatty() and sys.stdout.isatty() else None

    def read(self, message="car-agent >> "):
        if self.session is None:
            # 管道输入和重定向输出保持普通文本行为。
            return input(message)
        # 只在编辑输入时接管输出；工具执行和 input() 审批保持原行为。
        with patch_stdout():
            return self.session.prompt(message)
