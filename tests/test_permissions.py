"""权限管线的离线验证：不请求模型，不执行真实 Shell 命令。"""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


with patch("anthropic.Anthropic"):
    spec = importlib.util.spec_from_file_location(
        "agent_loop", Path(__file__).parents[1] / "src/car_agent/agent_loop.py"
    )
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)


class PermissionTests(unittest.TestCase):
    def run_calls(self, calls):
        responses = [
            SimpleNamespace(content=[
                SimpleNamespace(type="tool_use", id=str(i), name=name, input=args)
                for i, (name, args) in enumerate(calls)
            ]),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="完成")]),
        ]
        history = []
        with patch.object(agent.client.messages, "create", side_effect=responses):
            agent.agent_loop(history)
        return history[1]["content"]

    def test_hard_deny_never_asks_or_executes(self):
        handler = Mock()
        with patch.dict(agent.TOOL_HANDLERS, bash=handler), patch("builtins.input") as ask:
            result = self.run_calls([("bash", {"command": "sudo rm test.txt"})])
        ask.assert_not_called()
        handler.assert_not_called()
        self.assertTrue(result[0]["is_error"])

    def test_delete_requires_explicit_yes(self):
        for choice, allowed in [("", False), ("n", False), ("y", True)]:
            with self.subTest(choice=choice):
                handler = Mock(return_value="已执行")
                with patch.dict(agent.TOOL_HANDLERS, bash=handler), patch("builtins.input", return_value=choice):
                    result = self.run_calls([("bash", {"command": "rm test.txt"})])
                self.assertEqual(handler.call_count, int(allowed))
                self.assertEqual(result[0]["is_error"], not allowed)

    def test_paths_and_rejection_do_not_break_batch(self):
        with TemporaryDirectory() as workspace, TemporaryDirectory() as outside:
            root = Path(workspace).resolve()
            target = Path(outside).resolve() / "out.txt"
            with patch.object(agent, "WORKDIR", root), patch("builtins.input", return_value="n") as ask:
                results = self.run_calls([
                    ("write_file", {"path": str(target), "content": "拒绝写入"}),
                    ("write_file", {"path": "local.txt", "content": "允许写入"}),
                    ("read_file", {"path": "local.txt"}),
                ])
                ask.assert_called_once()
                self.assertFalse(target.exists())
                self.assertEqual(results[2]["content"], "允许写入")
                self.assertEqual([r["tool_use_id"] for r in results], ["0", "1", "2"])
            with patch.object(agent, "WORKDIR", root), patch("builtins.input", return_value="yes"):
                results = self.run_calls([("write_file", {"path": str(target), "content": "批准写入"})])
                self.assertFalse(results[0]["is_error"])
                self.assertEqual(target.read_text(), "批准写入")
            (root / "link").symlink_to(target)
            with patch.object(agent, "WORKDIR", root), patch("builtins.input", return_value="n") as ask:
                result = self.run_calls([("read_file", {"path": "link"})])
                ask.assert_called_once()
                self.assertTrue(result[0]["is_error"])

    def test_interrupted_approval_denies(self):
        for error in [EOFError, KeyboardInterrupt]:
            with patch("builtins.input", side_effect=error):
                self.assertFalse(agent.check_permission("bash", {"command": "rm test.txt"}))

    def test_destructive_word_matching(self):
        for command in ["del test.txt", "DEL test.txt", "echo ok; rm test.txt"]:
            self.assertIsNotNone(agent.check_rules("bash", {"command": command}))
        for command in ["model", "delimiter", "echo del test.txt", "ls"]:
            self.assertIsNone(agent.check_rules("bash", {"command": command}))


if __name__ == "__main__":
    unittest.main()
