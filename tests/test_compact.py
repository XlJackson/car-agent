import sys
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent import config
from car_agent.compact import ContextCompactor, dumps
from car_agent.hooks import HookRegistry


def call(name, id='1', **args):
    return NS(type='tool_use', id=id, name=name, input=args)


def response(text):
    return NS(content=[NS(type='text', text=text)])


def pair(id, content):
    return [{'role': 'assistant', 'content': [call('read_file', id, path='a.txt')]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': id, 'content': content, 'is_error': False}]}]


class CompactTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(config, 'WORKDIR', Path(self.temp.name).resolve())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = Mock()
        self.client.messages.create.return_value = response('事实摘要：已读取文件，仍需分析。')
        self.comp = ContextCompactor(self.client, '当前只读请求')

    def test_budget_saves_full_output_and_hostile_id_cannot_escape(self):
        messages = pair('../../escape', 'X' * 210000)
        prepared = self.comp.prepare(messages)
        files = list((config.WORKDIR/'.task_outputs/tool-results').glob('*.txt'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), 'X' * 210000)
        self.assertIn(str(files[0]), prepared[-1]['content'][0]['content'])
        self.assertEqual(len(messages[-1]['content'][0]['content']), 210000)
        self.client.messages.create.assert_not_called()

    def test_snip_keeps_tool_pairs_and_archives_serializable_sdk_objects(self):
        messages = [{'role': 'user', 'content': '起始请求'}]
        for i in range(35):
            messages += pair(str(i), 'short')
        prepared = self.comp.prepare(messages)
        pending = set()
        for message in prepared:
            for block in message['content'] if isinstance(message['content'], list) else []:
                if getattr(block, 'type', None) == 'tool_use':
                    pending.add(block.id)
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    self.assertIn(block['tool_use_id'], pending)
                    pending.remove(block['tool_use_id'])
        self.assertFalse(pending)
        self.assertIn('当前只读请求', dumps(prepared))
        transcript = next((config.WORKDIR/'.transcripts').glob('*.json'))
        self.assertEqual(len(json.loads(transcript.read_text())), len(messages))

    def test_micro_preserves_three_recent_and_unseen(self):
        messages=[]
        for i in range(6):
            messages += pair(str(i), str(i)*5000)
        self.comp.seen.update(['0','1','2','3','4'])
        prepared = self.comp.micro_compact(messages, 1)
        self.assertIn('saved at', prepared[1]['content'][0]['content'])
        self.assertEqual(prepared[5]['content'][0]['content'], '2'*5000)
        self.assertEqual(prepared[-1]['content'][0]['content'], '5'*5000)

    def test_fresh_large_result_fits_without_summary(self):
        messages=pair('fresh', 'A'*60000)
        prepared=self.comp.prepare(messages)
        self.assertIn('A'*1000, prepared[-1]['content'][0]['content'])
        self.assertLess(len(dumps(prepared)), self.comp.CONTEXT_CHAR_LIMIT)
        self.client.messages.create.assert_not_called()

    def test_summary_keeps_active_request_plan_and_chunks_input(self):
        self.comp.plan='[>] 检查剩余文件'
        messages=[{'role':'user','content':'旧背景'*22000}]
        prepared=self.comp.prepare(messages)
        self.assertIn('当前只读请求',prepared[0]['content'])
        self.assertIn('检查剩余文件',prepared[0]['content'])
        self.assertGreater(self.client.messages.create.call_count,1)
        for request in self.client.messages.create.call_args_list:
            self.assertNotIn('tools',request.kwargs)
            self.assertLess(len(request.kwargs['messages'][0]['content']),18000)

    def test_failed_summary_does_not_replace_history(self):
        messages=[{'role':'user','content':'old'*20000}]
        self.client.messages.create.side_effect=RuntimeError('summary failure')
        with self.assertRaises(RuntimeError):
            self.comp.prepare(messages)
        self.assertEqual(messages[0]['content'],'old'*20000)
        self.assertTrue(list((config.WORKDIR/'.transcripts').glob('*.json')))

    def test_manual_compact_waits_for_full_batch_and_preserves_stop_stats(self):
        snapshots=[]
        replies=iter([NS(content=[call('compact'),call('write_file','2',path='x.txt',content='written')]),response('已写入 x.txt，待核验'),response('完成')])
        def model(**kwargs):
            snapshots.append(json.loads(dumps(kwargs)))
            return next(replies)
        history=[{'role':'user','content':'写入文件后整理历史'}]
        hooks=HookRegistry()
        stop=Mock(return_value=None)
        hooks.register_hook('Stop',stop)
        with patch.object(agent.client.messages,'create',side_effect=model):
            agent.agent_loop(history,hooks=hooks,active_request='写入文件后整理历史')
        self.assertEqual((config.WORKDIR/'x.txt').read_text(),'written')
        summary_input=snapshots[1]['messages'][0]['content']
        self.assertIn('tool_use_id',summary_input)
        self.assertIn('written',summary_input)
        self.assertIn('[Compacted]',history[0]['content'])
        self.assertEqual(len(stop.call_args.args[0][1]['content']),2)

    def test_reactive_retry_once_other_errors_not_retried(self):
        for error in [RuntimeError('prompt_too_long'), RuntimeError('authentication failed')]:
            history=[{'role':'user','content':'请求'}]
            if 'prompt_too_long' in str(error):
                replies=[error,response('旧历史摘要'),error]
            else:
                replies=[error]
            with patch.object(agent.client.messages,'create',side_effect=replies) as model:
                with self.assertRaises(RuntimeError):
                    agent.agent_loop(history,hooks=HookRegistry())
            self.assertEqual(model.call_count,len(replies))

    def test_archive_directory_symlink_outside_rejected(self):
        with TemporaryDirectory() as outside:
            (config.WORKDIR/'.transcripts').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):
                self.comp.archive([])
