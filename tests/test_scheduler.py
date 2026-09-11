import sys
import json
import threading
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent import config, scheduler
from car_agent.scheduler import CronScheduler, cron_matches, validate_cron
from car_agent.permissions import UNATTENDED
from car_agent.hooks import create_default_hooks


class SchedulerTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        patcher=patch.object(config,'WORKDIR',self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path=self.root/'.scheduled_tasks.json'
        self.s=CronScheduler(self.path)
        self.addCleanup(self.s.close)

    def add(self, **kwargs):
        return json.loads(self.s.schedule('* * * * *','只读检查',**kwargs))['id']

    def test_validation_and_local_calendar(self):
        for cron in ['* * * *','60 * * * *','*/0 * * * *','0 24 * * *','0 9 * * 8','0 9 * * MON','0 9 * * 5-1']:
            with self.subTest(cron=cron),self.assertRaises(ValueError):
                validate_cron(cron)
        monday=datetime(2026,9,14,9,0)
        self.assertTrue(cron_matches('0 9 * * 1-5',monday))
        self.assertTrue(cron_matches('*/5 8-10 * * 1,3',monday))
        self.assertFalse(cron_matches('0 9 * * 0',monday))
        sunday=datetime(2026,9,13,9,0)
        self.assertTrue(cron_matches('0 9 * * 7',sunday))

    def test_persist_pending_restore_and_no_duplicate_minute(self):
        id=self.add(durable=True)
        self.add(durable=False)
        moment=datetime(2026,9,14,9,0)
        self.s.poll_due_jobs(moment)
        self.s.poll_due_jobs(moment)
        self.assertEqual(len(self.s.queue),2)
        restarted=CronScheduler(self.path)
        self.assertEqual(list(restarted.jobs),[id])
        self.assertEqual(list(restarted.queue),[id])
        restarted.take()
        restarted.acknowledge(id)
        restarted.poll_due_jobs(moment)
        self.assertFalse(restarted.queue)
        restarted.poll_due_jobs(datetime(2026,9,14,9,1))
        self.assertEqual(list(restarted.queue),[id])

    def test_due_save_failure_does_not_enqueue(self):
        id=self.add(durable=True)
        with patch.object(self.s,'save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.s.poll_due_jobs(datetime(2026,9,14,9,0))
        self.assertFalse(self.s.queue)
        self.assertFalse(self.s.jobs[id].pending_delivery)
        self.assertIsNone(self.s.jobs[id].last_fired)

    def test_cancel_queued_and_one_shot_ack(self):
        id=self.add(durable=True,recurring=False)
        self.s.poll_due_jobs(datetime(2026,9,14,9,0))
        self.s.cancel(id)
        self.assertIsNone(self.s.take())
        id=self.add(durable=True,recurring=False)
        self.s.poll_due_jobs(datetime(2026,9,14,9,0))
        self.s.take()
        self.s.acknowledge(id)
        self.assertNotIn(id,self.s.jobs)
        self.assertEqual(json.loads(self.path.read_text()),[])

    def test_agent_lock_and_failed_delivery_requeues(self):
        id=self.add()
        self.s.poll_due_jobs(datetime(2026,9,14,9,0))
        lock=threading.Lock()
        lock.acquire()
        callback=Mock()
        self.assertFalse(self.s.process_once(lock,callback))
        callback.assert_not_called()
        lock.release()
        self.s.process_once(lock,Mock(side_effect=RuntimeError('API')))
        self.assertIn(id,self.s.queue)
        self.assertTrue(self.s.jobs[id].pending_delivery)
        self.assertIsNone(self.s.take()) # backoff, not a busy retry loop
        self.s.retry_at[id]=0
        self.s.process_once(lock,lambda job,ack: ack())
        self.assertFalse(self.s.jobs[id].pending_delivery)

    def test_corrupt_storage_not_overwritten(self):
        self.path.write_text('{bad')
        with self.assertRaises(ValueError):
            CronScheduler(self.path)
        self.assertEqual(self.path.read_text(),'{bad')

    def test_unattended_approval_never_reads_terminal(self):
        token=UNATTENDED.set(True)
        handler=Mock()
        try:
            with patch('builtins.input') as ask:
                call=NS(name='bash',id='1',input={'command':'rm test.txt'})
                result=agent.execute_tool(call,create_default_hooks(),handlers={'bash':handler})
            self.assertTrue(result['is_error'])
            handler.assert_not_called()
            ask.assert_not_called()
        finally:
            UNATTENDED.reset(token)

    def test_scheduled_turn_isolated_no_memory_or_scheduler_tools(self):
        id=self.add()
        job=self.s.jobs[id]
        ack=Mock()
        def model(**kwargs):
            self.assertEqual(kwargs['messages'][-1]['content'],'[Scheduled] 只读检查')
            self.assertNotIn('schedule_cron',{t['name'] for t in kwargs['tools']})
            return NS(content=[NS(type='text',text='完成')])
        with patch.object(agent.client.messages,'create',side_effect=model), patch.object(agent,'MemoryManager') as memory:
            agent.run_scheduled_job(job,ack)
        ack.assert_called_once()
        memory.assert_not_called()
        self.assertFalse(UNATTENDED.get())

    def test_clock_and_delivery_threads_wake_without_user_input(self):
        self.add(recurring=False)
        delivered=threading.Event()
        def run(job, ack):
            ack()
            delivered.set()
        self.s.start(threading.Lock(),run)
        self.assertTrue(delivered.wait(2))
        self.s.close()
        self.assertTrue(all(not t.is_alive() for t in self.s.threads))
