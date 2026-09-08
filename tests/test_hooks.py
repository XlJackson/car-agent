"""离线验证 Hook 控制流和模型轮数上限。"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent.hooks import HookRegistry, create_default_hooks


def call(name='bash'):
    return NS(type='tool_use', id='tool-1', name=name, input={'command': 'echo hello'})


def answer(text='完成'):
    return NS(content=[NS(type='text', text=text)])


class HookTests(unittest.TestCase):
    def test_order_and_non_none_block(self):
        hooks = HookRegistry()
        events = []
        hooks.register_hook('PreToolUse', lambda *args: events.append('first'))
        hooks.register_hook('PreToolUse', lambda *args: '')
        hooks.register_hook('PreToolUse', lambda *args: events.append('unreachable'))
        hooks.register_hook('PostToolUse', lambda *args: events.append('post'))
        handler = Mock()
        with patch.dict(agent.TOOL_HANDLERS, bash=handler):
            result = agent.execute_tool(call(), hooks)
        self.assertTrue(result['is_error'])
        self.assertEqual(events, ['first'])
        handler.assert_not_called()

    def test_observer_return_does_not_skip_other_callbacks(self):
        for event in ['UserPromptSubmit', 'PostToolUse']:
            hooks = HookRegistry()
            seen = []
            hooks.register_hook(event, lambda: 'ignored')
            hooks.register_hook(event, lambda: seen.append('second'))
            self.assertIsNone(hooks.trigger_hooks(event))
            self.assertEqual(seen, ['second'])

    def test_pre_exception_blocks_but_post_exception_preserves_result(self):
        def broken(*args):
            raise RuntimeError('hook failed')
        handler = Mock(return_value='success')
        for event in ['PreToolUse', 'PostToolUse']:
            hooks = HookRegistry()
            hooks.register_hook(event, broken)
            with patch.dict(agent.TOOL_HANDLERS, bash=handler):
                result = agent.execute_tool(call(), hooks)
            self.assertEqual(result['is_error'], event == 'PreToolUse')
            if event == 'PostToolUse':
                self.assertEqual(result['content'], 'success')
        handler.assert_called_once()

    def test_failed_handler_still_triggers_post(self):
        hooks = HookRegistry()
        post = Mock()
        hooks.register_hook('PostToolUse', post)
        with patch.dict(agent.TOOL_HANDLERS, bash=Mock(side_effect=ValueError('failed'))):
            result = agent.execute_tool(call(), hooks)
        self.assertTrue(result['is_error'])
        post.assert_called_once_with('bash', {'command': 'echo hello'}, result)

    def test_stop_continuation_and_hard_cap(self):
        hooks = HookRegistry()
        hooks.register_hook('Stop', lambda messages: '再检查一次')
        history = []
        with patch.object(agent.client.messages, 'create', side_effect=[answer(), answer()]) as model:
            self.assertFalse(agent.agent_loop(history, hooks=hooks, max_rounds=2))
        self.assertEqual(model.call_count, 2)
        self.assertEqual(history[-1], {'role': 'user', 'content': '再检查一次'})

    def test_tool_loop_cap_keeps_results(self):
        hooks = HookRegistry()
        history = []
        with patch.object(agent.client.messages, 'create', return_value=NS(content=[call()])) as model:
            with patch.dict(agent.TOOL_HANDLERS, bash=Mock(return_value='ok')):
                self.assertFalse(agent.agent_loop(history, hooks=hooks, max_rounds=2))
        self.assertEqual(model.call_count, 2)
        self.assertEqual(history[-1]['content'][0]['tool_use_id'], 'tool-1')

    def test_prompt_context_and_task_local_stop_messages(self):
        hooks = HookRegistry()
        hooks.register_hook('UserPromptSubmit', lambda query, history: history.append(
            {'role': 'user', 'content': '车辆上下文（测试）'}))
        stop = Mock(return_value=None)
        hooks.register_hook('Stop', stop)
        history = [{'role': 'user', 'content': '上一轮提问'}]
        with patch.object(agent.client.messages, 'create', return_value=answer()):
            self.assertTrue(agent.submit_query('当前问题', history, hooks=hooks))
        self.assertEqual(history[1]['content'], '车辆上下文（测试）')
        self.assertEqual(history[2]['content'], '当前问题')
        self.assertEqual(len(stop.call_args.args[0]), 1)

    def test_default_registry_does_not_duplicate_callbacks(self):
        self.assertEqual(len(create_default_hooks().hooks['PreToolUse']), 1)
        self.assertEqual(len(create_default_hooks().hooks['PreToolUse']), 1)


if __name__ == '__main__':
    unittest.main()
