import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from car_agent import config
from car_agent.memory import MemoryManager, MemoryStore, memory_system


def record(name='indent-style', body='用户长期偏好使用 tab 缩进。'):
    return dict(name=name, type='user', description='缩进风格 tab', body=body,
                scope='persistent', evidence='我偏好 tab 缩进')


class MemoryTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher=patch.object(config,'WORKDIR',Path(self.temp.name).resolve())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager=MemoryManager(Mock())
        self.store=self.manager.store

    def test_persist_restart_index_and_duplicate(self):
        self.assertEqual(self.store.store_candidates([record()], '我偏好 tab 缩进'),1)
        self.assertEqual(self.store.store_candidates([record()], '我偏好 tab 缩进'),0)
        restarted=MemoryStore(config.WORKDIR/'.memory')
        self.assertEqual(restarted.records()[0]['body'],record()['body'])
        self.assertIn('indent-style.md',(config.WORKDIR/'.memory/MEMORY.md').read_text())

    def test_temporary_unproven_sensitive_and_traversal_rejected(self):
        bad=[{**record(),'scope':'current_task'}, {**record(),'body':'本次会话不创建文件'},
             {**record(),'evidence':'没说过'}, {**record(),'name':'../escape'},
             {**record(),'body':'password=abc123'}, {**record(),'body':''}]
        self.assertEqual(self.store.store_candidates(bad, '我偏好 tab 缩进'),0)
        self.assertFalse(self.store.records())

    def test_recall_selection_does_not_send_bodies(self):
        self.store.store_candidates([record(body='BODY_SECRET: 用户偏好 tab。')], '我偏好 tab 缩进')
        with patch.object(self.manager,'ask_json',return_value=[0]) as choose:
            recalled=self.manager.recall('我偏好什么缩进？')
        self.assertIn('BODY_SECRET',recalled)
        self.assertNotIn('BODY_SECRET',str(choose.call_args))
        self.assertIn('当前请求优先',memory_system('base',recalled))

    def test_selection_failure_keyword_fallback_and_limit(self):
        self.store.store_candidates([record()], '我偏好 tab 缩进')
        with patch.object(self.manager,'ask_json',side_effect=ValueError('bad JSON')):
            self.assertIn('tab',self.manager.recall('tab 缩进'))
            self.assertEqual(self.manager.recall('天气预报'),'')
        with patch.object(self.manager,'MAX_RECALL_CHARS',1), patch.object(self.manager,'ask_json',return_value=[0]):
            self.assertEqual(self.manager.recall('tab'),'')

    def test_optout_skips_extraction(self):
        with patch.object(self.manager,'ask_json') as model:
            self.manager.extract('不要记住这个偏好')
        model.assert_not_called()

    def test_consolidation_failure_restores_exact_snapshot(self):
        self.store.store_candidates([record()], '我偏好 tab 缩进')
        root=config.WORKDIR/'.memory'
        before={p.name:p.read_text() for p in root.glob('*.md')}
        with patch.object(self.store,'rebuild_index',side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.store.replace_records([record(name='merged')])
        self.assertEqual(before,{p.name:p.read_text() for p in root.glob('*.md')})
        self.assertTrue(list((root/'snapshots').glob('*/indent-style.md')))

    def test_consolidation_requires_all_sources(self):
        for i in range(10):
            self.store.write_record(record(name=f'fact-{i}',body=f'稳定事实 {i}'))
        incomplete=[{**record(),'sources':['fact-0']}]
        with patch.object(self.manager,'ask_json',return_value=incomplete):
            self.manager.consolidate()
        self.assertEqual(len(self.store.records()),10)
        merged=[{**record(),'sources':[f'fact-{i}' for i in range(10)]}]
        with patch.object(self.manager,'ask_json',return_value=merged):
            self.manager.consolidate()
        self.assertEqual(len(self.store.records()),1)

    def test_symlink_memory_directory_blocked(self):
        with TemporaryDirectory() as outside:
            (config.WORKDIR/'.memory').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):
                self.store.write_record(record())
