"""计划校验、轮次提醒和终端状态的离线测试。"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent.hooks import HookRegistry
from car_agent.todo import TodoManager


def item(status='pending', content='创建测试文件'):
    return {'content': content, 'status': status}


def call(name, **args):
    return NS(type='tool_use', id='id-' + name, name=name, input=args)


class TodoTests(unittest.TestCase):
    def test_empty_text_beside_todo_is_not_sent_back(self):
        from anthropic.types import TextBlock, ToolUseBlock
        tool = ToolUseBlock(type='tool_use', id='todo-1', name='todo_write', input={'todos': [item()]})
        responses = iter([
            NS(content=[TextBlock(type='text', text=''), tool, TextBlock(type='text', text='  ')]),
            NS(content=[TextBlock(type='text', text='计划已创建')]),
        ])
        def model(**kwargs):
            # 在下一次请求发生时检查历史，而非只看最后的列表。
            for message in kwargs['messages']:
                for block in message['content'] if isinstance(message['content'], list) else []:
                    if getattr(block, 'type', None) == 'text':
                        self.assertTrue(block.text.strip())
            return next(responses)
        history = []
        with patch.object(agent.client.messages, 'create', side_effect=model):
            self.assertTrue(agent.agent_loop(history, hooks=HookRegistry()))
        self.assertEqual(history[0]['content'], [tool])
        self.assertEqual(history[1]['content'][0]['tool_use_id'], 'todo-1')

    def test_empty_response_fallback_preserves_other_block_types(self):
        self.assertTrue(agent.clean_response_content([])[0].text.strip())
        self.assertTrue(agent.clean_response_content([{'type': 'text', 'text': ''}])[0].text.strip())
        thinking = {'type': 'thinking', 'thinking': '', 'signature': 'keep'}
        text = {'type': 'text', 'text': '保留正文'}
        self.assertEqual(agent.clean_response_content([thinking, text]), [thinking, text])

    def test_valid_string_forms_and_render(self):
        manager = TodoManager()
        for payload in [[item()], '[{"content": "创建文件", "status": "pending"}]', "[{'content': '创建文件', 'status': 'pending'}]"]:
            self.assertIn('[ ] 待开始', manager.update(payload))
        self.assertIn('[>] 进行中', manager.update([item('in_progress')]))
        self.assertIn('已完成 1/1', manager.update([item('completed')]))
        self.assertIn('暂无步骤', manager.update([]))

    def test_invalid_updates_are_atomic(self):
        manager = TodoManager()
        manager.update([item()])
        invalid = [[item()] * 21, [item(content=' ')], [item('invalid')],
                   [item('in_progress')] * 2, [item(content='x' * 201)],
                   [{'content': 'missing status'}], {}, '__import__("os").getcwd()']
        for payload in invalid:
            with self.subTest(payload=repr(payload)[:50]), self.assertRaises(ValueError):
                manager.update(payload)
            self.assertEqual(manager.items, [item()])

    def test_terminal_updates_only_on_change(self):
        manager = TodoManager()
        out = io.StringIO()
        with redirect_stdout(out):
            manager.write([item()])
            manager.write([item()])
            manager.write([item('in_progress')])
            manager.write([item('completed')])
        self.assertEqual(out.getvalue().count('任务计划'), 3)

    def test_reminder_counts_rounds_and_repeats(self):
        manager = TodoManager()
        calls = [call('bash')] * 4
        results = [{'is_error': False}] * 4
        for round_number in range(1, 7):
            reminder = manager.after_tool_round(calls, results)
            self.assertEqual(reminder is not None, round_number % 3 == 0)

    def test_only_successful_todo_resets_counter(self):
        manager = TodoManager()
        manager.rounds_since_todo = 2
        self.assertIsNotNone(manager.after_tool_round([call('todo_write')], [{'is_error': True}]))
        manager.rounds_since_todo = 2
        self.assertIsNone(manager.after_tool_round([call('todo_write')], [{'is_error': False}]))
        self.assertEqual(manager.rounds_since_todo, 0)

    def test_real_dispatch_displays_plan_and_updates_after_execution(self):
        responses = [
            NS(content=[call('todo_write', todos=[item('in_progress')]), call('bash', command='echo ok')]),
            NS(content=[call('todo_write', todos=[item('completed')])]),
            NS(content=[NS(type='text', text='完成')]),
        ]
        history = []
        handler = Mock(return_value='ok')
        out = io.StringIO()
        with redirect_stdout(out), patch.object(agent.client.messages, 'create', side_effect=responses):
            with patch.dict(agent.TOOL_HANDLERS, bash=handler):
                self.assertTrue(agent.agent_loop(history, hooks=HookRegistry()))
        handler.assert_called_once_with(command='echo ok')
        self.assertIn('[>] 进行中', out.getvalue())
        self.assertIn('[x] 已完成', out.getvalue())
        self.assertEqual(len(history[1]['content']), 2)
        self.assertFalse(history[1]['content'][0]['is_error'])

    def test_reminder_inserted_after_tool_results_and_fresh_task(self):
        responses = [NS(content=[call('bash', command='echo ok')])] * 3
        history = []
        with patch.object(agent.client.messages, 'create', side_effect=responses):
            with patch.dict(agent.TOOL_HANDLERS, bash=Mock(return_value='ok')):
                self.assertFalse(agent.agent_loop(history, hooks=HookRegistry(), max_rounds=3))
        self.assertEqual(history[-1]['content'][0]['type'], 'tool_result')
        self.assertEqual(history[-1]['content'][1]['type'], 'text')
        self.assertEqual(len(history[1]['content']), 1)
        with patch.object(agent.client.messages, 'create', return_value=responses[0]):
            with patch.dict(agent.TOOL_HANDLERS, bash=Mock(return_value='ok')):
                agent.agent_loop(history, hooks=HookRegistry(), max_rounds=1)
        self.assertEqual(len(history[-1]['content']), 1)

    def test_early_stop_reports_unfinished_and_next_task_is_empty(self):
        out = io.StringIO()
        responses = [NS(content=[call('todo_write', todos=[item()])]), NS(content=[])]
        with redirect_stdout(out), patch.object(agent.client.messages, 'create', side_effect=responses):
            agent.agent_loop([], hooks=HookRegistry())
        self.assertIn('仍有 1 项未标记完成', out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), patch.object(agent.client.messages, 'create', return_value=NS(content=[])):
            agent.agent_loop([], hooks=HookRegistry())
        self.assertNotIn('未标记完成', out.getvalue())


if __name__ == '__main__':
    unittest.main()
