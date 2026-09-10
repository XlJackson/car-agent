import sys
import signal
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent.background import BackgroundManager
from car_agent.hooks import HookRegistry, create_default_hooks


class BackgroundTests(TestCase):
    def manager(self, **kwargs):
        manager=BackgroundManager(**kwargs)
        self.addCleanup(manager.close)
        return manager

    def finish(self, manager):
        for task in list(manager.tasks.values()):
            task['worker'].join(timeout=3)
            self.assertFalse(task['worker'].is_alive())

    def test_success_failure_and_once_only_notification(self):
        manager=self.manager()
        manager.start('printf hello', 'a')
        manager.start('exit 7', 'a')
        self.finish(manager)
        self.assertEqual(manager.collect('b'),[])
        notices=manager.collect('a')
        self.assertEqual(len(notices),2)
        self.assertIn('hello',''.join(notices))
        self.assertIn('"exit_code": 7',''.join(notices))
        self.assertNotIn('tool_use_id',''.join(notices))
        self.assertEqual(manager.collect('a'),[])

    def test_timeout_and_close_cleanup(self):
        manager=self.manager(timeout=.05)
        manager.start('sleep 30')
        self.finish(manager)
        self.assertIn('已终止',manager.collect()[0])
        manager=self.manager()
        manager.start('sleep 30')
        pid=next(iter(manager.tasks.values()))['process'].pid
        manager.close()
        self.finish(manager)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid,0)
        self.assertIn('cancelled',manager.collect()[0])

    def test_capacity_and_transfer(self):
        manager=self.manager(max_running=1)
        manager.start('sleep 30','child')
        with self.assertRaises(RuntimeError):
            manager.start('echo another')
        manager.transfer('child','parent')
        self.assertFalse(manager.pending('child'))
        self.assertEqual(len(manager.pending('parent')),1)

    def test_permission_before_start_and_sync_default(self):
        manager=Mock()
        call=NS(name='bash',id='1',input={'command':'sudo test','run_in_background':True})
        with patch.object(agent,'BACKGROUND',manager):
            result=agent.execute_tool(call,create_default_hooks())
        self.assertTrue(result['is_error'])
        manager.start.assert_not_called()
        handler=Mock(return_value='sync')
        call.input={'command':'echo ok','run_in_background':False}
        with patch.object(agent,'BACKGROUND',manager):
            result=agent.execute_tool(call,HookRegistry(),handlers={'bash':handler})
        handler.assert_called_once_with(command='echo ok')
        self.assertEqual(result['content'],'sync')

    def test_other_tool_runs_while_background_waits(self):
        manager=self.manager()
        call=NS(name='bash',id='1',input={'command':'sleep 30','run_in_background':True})
        with patch.object(agent,'BACKGROUND',manager):
            result=agent.execute_tool(call,HookRegistry())
            self.assertIn('尚未完成',result['content'])
            other=Mock(return_value='read done')
            read=NS(name='read_file',id='2',input={'path':'a.txt'})
            result=agent.execute_tool(read,HookRegistry(),handlers={'read_file':other})
        self.assertEqual(result['content'],'read done')
        self.assertTrue(manager.pending())

    def test_notification_injected_as_new_event_before_request(self):
        manager=Mock()
        manager.collect.return_value=['<task_notification>done</task_notification>']
        manager.pending.return_value=[]
        def model(**kwargs):
            self.assertIn('task_notification',kwargs['messages'][-1]['content'])
            return NS(content=[NS(type='text',text='收到结果')])
        with patch.object(agent,'BACKGROUND',manager), patch.object(agent.client.messages,'create',side_effect=model):
            self.assertTrue(agent.agent_loop([{'role':'user','content':'看看结果'}],hooks=HookRegistry()))
