"""Metadata, billing boundary and receipt tests; no provider requests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from response_growth_cases.runtimes import RuntimeCases
from response_growth_cases.hooks import HookCases
from response_growth_cases.forks import ForkCases
from osk import response_growth as rg, worker_preflight  # noqa: E402
import test_growth as base_tests  # noqa: E402


class ResponseGrowthTests(RuntimeCases, HookCases, ForkCases, unittest.TestCase):
    def setUp(self):
        # Preflight reads the native home/system config; never the developer's own proxy or trust list.
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environ = patch.dict(rg.os.environ, {'HOME': home.name, 'USERPROFILE': home.name, 'ProgramData': home.name,
                                             'CODEX_HOME': str(Path(home.name) / '.codex')})
        environ.start()
        self.addCleanup(environ.stop)
        # The native hooks/list query runs the real CLI; test_codex_preflight_requires_* drives it.
        hooks = patch.object(worker_preflight, '_codex_hook_ready', return_value=True)
        self.hook_ready = hooks.start()
        self.addCleanup(hooks.stop)


class HookBudgetTests(unittest.TestCase):
    """Hosts cut hook text (Claude Code swaps >10,000 chars for a 2KB preview, Kiro keeps 3,000):
    whole blocks fold, lowest keep first, and one line says where they are."""

    def test_assemble_folds_whole_blocks_lowest_first(self):
        from osk.hook_text import Block, assemble
        shown = []
        blocks = [Block('banner', '[osk 정돈이 밀렸다 — x]'),
                  Block('memory', 'M' * 300, 95, label='기억', pull='`scope_memory(session="k")`',
                        on_shown=lambda: shown.append('memory')),
                  Block('organization', 'O' * 300, 60, label='조직 검토', pull='overview:organization'),
                  Block('tidy', 'T' * 300, 80, label='정돈', pull='overview:tidy',
                        on_shown=lambda: shown.append('tidy'))]
        text, folded = assemble(blocks, 10_000, session='k')
        self.assertEqual((folded, shown), (set(), ['memory', 'tidy']))
        self.assertNotIn('[osk 접음', text)
        shown.clear()
        text, folded = assemble(blocks, 700, session='k')
        self.assertLessEqual(len(text), 700)
        self.assertEqual(folded, {'organization', 'tidy'})
        self.assertEqual(shown, ['memory'], 'a folded block was not delivered')
        self.assertTrue(text.startswith('[osk 정돈이 밀렸다'))
        self.assertIn('M' * 300, text)
        self.assertNotIn('T' * 10, text)
        self.assertTrue(text.endswith('[osk 접음 — 훅 한도 700자 안에 싣지 못한 것: 조직 검토 · 정돈. '
                                      '읽는 곳: `overview(session="k", include=["organization", "tidy"])`]'), text)

    def test_unfoldable_blocks_alone_are_cut_at_a_line(self):
        from osk.hook_text import Block, assemble
        text, _ = assemble([Block('lead', 'line\n' * 100)], 120)
        self.assertLessEqual(len(text), 120)
        self.assertTrue(text.endswith('\n[osk 생략 — 훅 한도]'), text)

    def test_start_keeps_its_warning_when_organization_cannot_be_read(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            env = {'harness':'claude','session_id':'own','cwd':str(VAULT.root),'session':'own'}
            idle = {'harness':'claude','conversation_id':'own','pending':False,'capture_error':None,
                    'capture_recovery':{'state':'ready'}}
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('subscription login required')), \\
                    patch.object(conversation_capture,'hook_capture',return_value=idle), \\
                    patch.object(hook,'_organization_jobs',side_effect=ValueError('damaged organization state')):
                start = hook.capture_block(VAULT, env,'own',startup=True)
            assert '검토 경고' in start and '참조·조직 검토 판독 진단' in start, start
            assert 'damaged organization state' in start and '포착·통합 진단' not in start, start
        ''')

    def test_hosts_declare_their_limit(self):
        from osk import harness
        self.assertEqual(harness.get('kiro').hook_budget, 2_800)
        self.assertEqual({harness.get(n).hook_budget for n in ('claude', 'codex')}, {9_500})

    def test_review_turn_carries_memory_once_per_hash(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', 'first memory sentinel', space='00_Scope/W1')
            from osk import write
            write.set_primary(VAULT, 'own', 'W1', 'test')
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n'+
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('subscription login required')):
                rg.probe(VAULT, env)
                hook.capture_block(VAULT, env,'own',startup=True)
                seen = {}
                for n in range(1,25):
                    if n == 20:
                        scope_memory.replace(VAULT, 'own', 'second memory sentinel', scope_memory.read(VAULT, 'own')['hash'])
                    seen[n] = hook.capture_block(VAULT, env,'own')
            assert 'first memory sentinel' in seen[9], seen[9]
            assert 'first memory sentinel' not in seen[15] and 'scope_memory(session="own")' in seen[15], seen[15]
            assert '출처·허브를 완성한다' in seen[15], 'the one line keeps the node-first guidance'
            assert 'second memory sentinel' in seen[24], 'a changed hash carries the whole text again'
            # Kiro's limit folds whole blocks; the review itself is pulled by command.
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('subscription login required')):
                while conversation_capture.status(VAULT, 'claude','own')['prompt_count'] < 29:
                    conversation_capture.tick(VAULT, 'claude','own')
                small = hook.capture_block(VAULT, env,'own',budget=2800)
            assert len(small) <= 2800 and '[osk 케이던스 — user 턴 30]' in small, small
            # The one line can fold too (Kiro, 2026-09-27): the folded line still names it and where to read it.
            from osk.hook_text import assemble
            line = hook._memory(VAULT, scope_memory, 'own', ('claude','own'), gate=True, keep=85)
            assert '이 대화에 앞서 실은 전문과' in line.text and line.label.startswith('scope 기억'), line
            folded, gone = assemble([line], 200, session='own')
            assert gone == {'memory'} and 'scope_memory(session="own")' in folded, folded
        ''')

    def test_review_turn_carries_bound_scope_memory_without_a_primary(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, write, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', 'bound memory sentinel', space='00_Scope/W1')
            assert write.bindings(VAULT, 'own') == {'scopes': ['W1'], 'primary': None, 'undetermined': False}
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n'+
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('subscription login required')):
                rg.probe(VAULT, env)
                hook.capture_block(VAULT, env,'own',startup=True)
                rg.after_hook(VAULT, env)   # the hooks' worker captures (#192)
                seen = {n: hook.capture_block(VAULT, env,'own') for n in range(1,16)}
            assert 'bound memory sentinel' in seen[9], seen[9]
            assert 'space="00_Scope/W1"' in seen[9], seen[9]          # 전문 블록의 수정 안내도 space를 명시한다
            assert 'bound memory sentinel' not in seen[15], seen[15]
            assert 'scope_memory(session="own", space="00_Scope/W1")' in seen[15], seen[15]
            assert '주된 scope 없음' in seen[9], seen[9]
        ''')


if __name__ == '__main__':
    unittest.main()
