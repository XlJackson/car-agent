import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent import mcp_tools
from car_agent.mcp_tools import MCPManager, StdioConnection, external_name, result_text
from car_agent.hooks import create_default_hooks, HookRegistry
from car_agent.permissions import UNATTENDED, check_permission


class MCPTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mcp_servers.json'
        demo = Path(__file__).parents[1] / 'examples/mcp_demo_server.py'
        self.entry = {'command': '${PYTHON}', 'args': [str(demo.resolve())],
                      'tool_policy': {'add': 'allow', 'get_version': 'allow'}}
        self.path.write_text(json.dumps({'servers': {'demo': self.entry}}))
        self.manager = MCPManager(self.path)
        self.addCleanup(self.manager.close)
        patcher = patch.object(mcp_tools, 'MANAGER', self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_stdio_discovery_dispatch_errors_and_cleanup(self):
        self.assertIn('mcp__demo__add', self.manager.connect('demo'))
        connection = self.manager.connections['demo']
        self.assertIn('已连接', self.manager.connect('demo'))
        self.assertIs(self.manager.connections['demo'], connection)
        schemas, handlers = self.manager.assemble([], {})
        self.assertEqual(len(schemas), 2)
        self.assertIn('42', handlers['mcp__demo__add'](a=20, b=22))
        self.assertIn('car-agent-demo 1.0', handlers['mcp__demo__get_version']())
        bad = NS(id='bad', name='mcp__demo__add', input={'a': 1})
        result = agent.execute_tool(bad, create_default_hooks(), handlers=handlers)
        self.assertTrue(result['is_error'])
        self.assertEqual(result['tool_use_id'], 'bad')
        self.manager.close()
        self.assertFalse(connection.thread.is_alive())

    def test_loop_discovers_tools_only_on_following_round(self):
        calls = [NS(content=[NS(type='tool_use', name='connect_mcp', id='1', input={'name': 'demo'})]),
                 NS(content=[NS(type='tool_use', name='mcp__demo__add', id='2', input={'a': 1, 'b': 2})]),
                 NS(content=[NS(type='text', text='结果是 3')])]
        self.manager.servers['demo']['connect_policy'] = 'allow'
        history = []
        with patch.object(agent.client.messages, 'create', side_effect=calls) as create:
            self.assertTrue(agent.agent_loop(history))
        initial = {t['name'] for t in create.call_args_list[0].kwargs['tools']}
        later = {t['name'] for t in create.call_args_list[1].kwargs['tools']}
        self.assertIn('connect_mcp', initial)
        self.assertNotIn('mcp__demo__add', initial)
        self.assertIn('mcp__demo__add', later)
        self.assertFalse(history[3]['content'][0]['is_error'])

    def test_host_policy_denial_and_unattended_confirmation(self):
        with patch('builtins.input', return_value='n') as ask:
            self.assertFalse(check_permission('connect_mcp', {'name': 'demo'}))
            ask.assert_called_once()
        self.manager.origins['mcp__demo__unknown'] = ('demo', 'unknown')
        token = UNATTENDED.set(True)
        try:
            with patch('builtins.input') as ask:
                self.assertFalse(check_permission('mcp__demo__unknown', {}))
                ask.assert_not_called()
        finally:
            UNATTENDED.reset(token)
        self.manager.servers['demo']['tool_policy']['unknown'] = 'deny'
        handler = Mock()
        result = agent.execute_tool(NS(name='mcp__demo__unknown', id='x', input={}),
                                    create_default_hooks(), handlers={'mcp__demo__unknown': handler})
        self.assertTrue(result['is_error'])
        handler.assert_not_called()
        self.assertFalse(check_permission('mcp__forged__read_only', {}))

    def test_normalized_collision_rolls_back_connection(self):
        connection = Mock(tools=[{'name': 'get.version', 'inputSchema': {'type': 'object'}},
                                 {'name': 'get_version', 'inputSchema': {'type': 'object'}}])
        with patch.object(mcp_tools, 'StdioConnection', return_value=connection):
            with self.assertRaisesRegex(ValueError, '冲突'):
                self.manager.connect('demo')
        connection.close.assert_called_once()
        self.assertFalse(self.manager.connections)
        self.assertFalse(self.manager.definitions)
        with self.assertRaises(ValueError):
            external_name('x' * 64, 'a')

    def test_handler_binding_and_parameter_names(self):
        self.manager.origins.update({'mcp__demo__a': ('demo', 'a'), 'mcp__demo__b': ('demo', 'b')})
        for name in self.manager.origins:
            self.manager.definitions[name] = {'name': name, 'input_schema': {'type': 'object'}}
        _, handlers = self.manager.assemble([], {})
        with patch.object(self.manager, 'call', return_value='ok') as call:
            handlers['mcp__demo__a'](name='value', tool='value')
            call.assert_called_with('mcp__demo__a', {'name': 'value', 'tool': 'value'})
            handlers['mcp__demo__b']()
            call.assert_called_with('mcp__demo__b', {})

    def test_empty_and_nontext_results_are_explicit(self):
        self.assertTrue(result_text(NS(content=[], structuredContent=None, isError=False)))
        result = NS(content=[NS(type='image')], structuredContent={'ok': True}, isError=False)
        self.assertIn('省略 image', result_text(result))
        result.isError = True
        with self.assertRaises(RuntimeError):
            result_text(result)

    def test_child_and_scheduled_have_no_connect_tool(self):
        for child, unattended in [(True, False), (False, True)]:
            token = UNATTENDED.set(unattended)
            try:
                with patch.object(agent.client.messages, 'create', return_value=NS(content=[NS(type='text', text='ok')])) as create:
                    agent.agent_loop([], is_subagent=child, hooks=HookRegistry())
                self.assertNotIn('connect_mcp', {t['name'] for t in create.call_args.kwargs['tools']})
            finally:
                UNATTENDED.reset(token)

    def test_invalid_configuration_fails_without_spawning(self):
        self.path.write_text('{broken')
        with patch.object(mcp_tools, 'StdioConnection') as connection:
            with self.assertRaises(ValueError):
                MCPManager(self.path)
            connection.assert_not_called()

    def test_unresponsive_server_times_out_and_stops_worker(self):
        from mcp import StdioServerParameters
        before = set(threading.enumerate())
        params = StdioServerParameters(command=sys.executable, args=['-c', 'import time; time.sleep(60)'])
        with self.assertRaises(Exception):
            StdioConnection(params, timeout=.1)
        self.assertFalse([t for t in threading.enumerate() if t not in before and t.name == 'mcp-stdio'])
