"""s06 离线验证：真实嵌套循环，模拟模型和工具。"""
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent import config
from car_agent.hooks import HookRegistry, create_default_hooks


def call(name, **args):
    return NS(type='tool_use', name=name, id='id-' + name, input=args)


def response(*blocks):
    return NS(content=list(blocks))


def text(value):
    return NS(type='text', text=value)


class SubagentTests(unittest.TestCase):
    def test_isolated_history_summary_only_shared_hooks(self):
        snapshots = []
        responses = iter([
            response(call('task', prompt='查找测试框架')),
            response(text(''), call('read_file', path='README.md')),
            response(text('测试框架为 unittest')),
            response(text('已确认')),
        ])
        def model(**kwargs):
            snapshots.append(deepcopy(kwargs))
            return next(responses)
        hooks = create_default_hooks()
        pre, post, stop, submit = Mock(), Mock(), Mock(return_value=None), Mock()
        pre.return_value = post.return_value = submit.return_value = None
        hooks.register_hook('PreToolUse', pre)
        hooks.register_hook('PostToolUse', post)
        hooks.register_hook('Stop', stop)
        hooks.register_hook('UserPromptSubmit', submit)
        history = [{'role': 'user', 'content': '父对话独有信息'}]
        with patch.object(agent.client.messages, 'create', side_effect=model):
            with patch.dict(agent.TOOL_HANDLERS, read_file=Mock(return_value='中间原始文件内容')):
                self.assertTrue(agent.agent_loop(history, hooks=hooks))
        self.assertEqual(snapshots[1]['messages'], [{'role': 'user', 'content': '查找测试框架'}])
        self.assertEqual({t['name'] for t in snapshots[1]['tools']}, {'bash', 'read_file', 'write_file', 'edit_file', 'glob', 'load_skill', 'compact'})
        self.assertNotIn('中间原始文件内容', repr(history))
        self.assertEqual(history[2]['content'][0]['content'], '测试框架为 unittest')
        self.assertEqual([args.args[0] for args in pre.call_args_list], ['task', 'read_file'])
        self.assertEqual([args.args[0] for args in post.call_args_list], ['read_file', 'task'])
        self.assertEqual(stop.call_count, 2)
        submit.assert_called_once()
        self.assertEqual(len(snapshots[2]['messages'][1]['content']), 1)

    def test_child_cannot_call_task_even_if_model_requests_it(self):
        history = []
        replies = [response(call('task', prompt='递归')), response(text('无法递归'))]
        with patch.object(agent.client.messages, 'create', side_effect=replies):
            self.assertTrue(agent.agent_loop(history, hooks=HookRegistry(), is_subagent=True))
        self.assertTrue(history[1]['content'][0]['is_error'])
        self.assertIn('未知工具', history[1]['content'][0]['content'])

    def test_child_permission_denial_prevents_execution(self):
        handler = Mock()
        replies = [response(call('task', prompt='执行命令')), response(call('bash', command='sudo test')), response(text('已拒绝')), response(text('停止'))]
        with patch.object(agent.client.messages, 'create', side_effect=replies):
            with patch.dict(agent.TOOL_HANDLERS, bash=handler), patch('builtins.input') as ask:
                agent.agent_loop([], hooks=create_default_hooks())
        handler.assert_not_called()
        ask.assert_not_called()

    def test_shared_workspace_child_write_parent_read(self):
        with TemporaryDirectory() as directory, patch.object(config, 'WORKDIR', Path(directory).resolve()):
            replies = [response(call('task', prompt='写入文件')), response(call('write_file', path='child.txt', content='shared')), response(text('文件已写入')), response(call('read_file', path='child.txt')), response(text('验证成功'))]
            history = []
            with patch.object(agent.client.messages, 'create', side_effect=replies):
                agent.agent_loop(history, hooks=create_default_hooks())
            self.assertEqual(history[3]['content'][0]['content'], 'shared')

    def test_child_limit_and_api_failure_become_parent_tool_errors(self):
        for failure in ['limit', 'api']:
            replies = [response(call('task', prompt='子任务')),
                       response(call('glob', pattern='*.py')) if failure == 'limit' else RuntimeError('服务失败'),
                       response(text('子任务未完成'))]
            history = []
            with patch.object(config, 'SUB_MAX_ROUNDS', 1), patch.object(agent.client.messages, 'create', side_effect=replies):
                with patch.dict(agent.TOOL_HANDLERS, glob=Mock(return_value='a.py')):
                    agent.agent_loop(history, hooks=HookRegistry())
            self.assertTrue(history[1]['content'][0]['is_error'])
            self.assertIn('上限' if failure == 'limit' else '服务失败', history[1]['content'][0]['content'])


if __name__ == '__main__':
    unittest.main()
