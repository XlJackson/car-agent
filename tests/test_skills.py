import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
with patch('anthropic.Anthropic'):
    from car_agent import agent_loop as agent
from car_agent import skill_loader
from car_agent.skill_loader import SkillLoader
from car_agent.hooks import HookRegistry


class SkillTests(TestCase):
    def write_skill(self, root, folder, text):
        directory = Path(root) / folder
        directory.mkdir()
        (directory / 'SKILL.md').write_text(text)

    def test_catalog_frontmatter_and_snapshot(self):
        with TemporaryDirectory() as root:
            content = '---\nname: sample\ndescription: >\n  审查代码\n  并报告依据\n---\nBODY_SENTINEL\n'
            self.write_skill(root, 'sample', content)
            loader = SkillLoader(Path(root))
            self.assertIn('审查代码 并报告依据', loader.catalog())
            self.assertNotIn('BODY_SENTINEL', loader.catalog())
            self.assertEqual(loader.load('sample'), content)
            (Path(root)/'sample/SKILL.md').write_text('modified')
            self.assertEqual(loader.load('sample'), content)
            with self.assertRaises(ValueError):
                loader.load('../sample/SKILL.md')

    def test_invalid_duplicate_fallback_and_symlink(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside:
            self.write_skill(root, 'a', '---\nname: duplicate\ndescription: first\n---\nfirst body')
            self.write_skill(root, 'b', '---\nname: duplicate\ndescription: second\n---\nsecond body')
            self.write_skill(root, 'bad', '---\nname: [\n---\ninvalid')
            self.write_skill(root, 'fallback', '# 标题描述\n正文')
            self.write_skill(outside, 'external', '---\nname: external\ndescription: outside\n---\nsecret')
            (Path(root)/'link').symlink_to(Path(outside)/'external', target_is_directory=True)
            loader = SkillLoader(Path(root))
            self.assertEqual(set(loader.skills), {'duplicate', 'fallback'})
            self.assertIn('first body', loader.load('duplicate'))
            self.assertIn('标题描述', loader.catalog())
            self.assertEqual(SkillLoader(Path(root)/'absent').catalog(), '（暂无技能）')

    def test_full_content_only_enters_request_after_load_parent_and_child(self):
        for is_subagent in [False, True]:
            with self.subTest(child=is_subagent), TemporaryDirectory() as root:
                content = '---\nname: sample\ndescription: 简短简介\n---\nBODY_SENTINEL'
                self.write_skill(root, 'sample', content)
                loader = SkillLoader(Path(root))
                snapshots = []
                replies = iter([NS(content=[NS(type='tool_use', id='skill1', name='load_skill', input={'name': 'sample'})]), NS(content=[NS(type='text', text='已阅读')])])
                def model(**kwargs):
                    snapshots.append(deepcopy(kwargs))
                    return next(replies)
                history = [{'role': 'user', 'content': '审查代码'}]
                with patch.object(skill_loader, 'SKILL_LOADER', loader), patch.object(agent.client.messages, 'create', side_effect=model):
                    agent.agent_loop(history, hooks=HookRegistry(), is_subagent=is_subagent)
                self.assertIn('sample: 简短简介', snapshots[0]['system'])
                self.assertNotIn('BODY_SENTINEL', snapshots[0]['system'])
                self.assertNotIn('BODY_SENTINEL', repr(snapshots[0]['messages']))
                self.assertIn('BODY_SENTINEL', repr(snapshots[1]['messages']))
                self.assertEqual(history[2]['content'][0]['tool_use_id'], 'skill1')

    def test_unknown_skill_returns_tool_error(self):
        call = NS(type='tool_use', id='bad', name='load_skill', input={'name': '../secret'})
        result = agent.execute_tool(call, HookRegistry())
        self.assertTrue(result['is_error'])
        self.assertIn('未知技能', result['content'])
