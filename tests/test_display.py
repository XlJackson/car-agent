import io
import sys
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent.display import tool_target
from car_agent.hooks import HookRegistry
from car_agent.todo import TodoManager


class DisplayTests(TestCase):
    def test_target_is_bounded_and_omits_write_body(self):
        self.assertEqual(tool_target('write_file', {'path': 'a.txt', 'content': 'private-body'}), 'a.txt')
        self.assertEqual(tool_target('edit_file', {'path': 'a.txt', 'old_text': 'secret', 'new_text': 'secret'}), 'a.txt')
        target = tool_target('bash', {'command': 'x' * 2000})
        self.assertLess(len(target), 200)
        self.assertIn('已截断', target)
        self.assertNotIn('\n', tool_target('task', {'prompt': 'a\nb'}))

    def test_intermediate_text_visible_final_once_and_errors_bounded(self):
        replies = [NS(content=[NS(type='text', text='结构说明正文'),
                               NS(type='tool_use', name='read_file', id='1', input={'path': 'a.py'})]),
                   NS(content=[NS(type='text', text='最终结论')])]
        output = io.StringIO()
        with redirect_stdout(output), patch.object(agent.client.messages, 'create', side_effect=replies):
            with patch.dict(agent.TOOL_HANDLERS, read_file=Mock(side_effect=ValueError('不存在'))):
                agent.agent_loop([], hooks=HookRegistry())
        self.assertIn('结构说明正文', output.getvalue())
        self.assertEqual(output.getvalue().count('最终结论'), 1)
        self.assertIn('read_file：a.py', output.getvalue())
        self.assertIn('失败/拒绝', output.getvalue())

    def test_skip_warning_returned_without_reexecution_or_rejection(self):
        todo = TodoManager()
        todo.update([{'content': '核对关系', 'status': 'pending'}])
        with redirect_stdout(io.StringIO()) as output:
            result = todo.write([{'content': '核对关系', 'status': 'completed'}])
        self.assertIn('从待开始直接标为完成', result)
        self.assertIn('计划提示', output.getvalue())
        self.assertEqual(todo.items[0]['status'], 'completed')
