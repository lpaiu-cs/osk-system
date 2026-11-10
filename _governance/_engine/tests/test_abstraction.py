"""v9 input selection, receipt truthfulness and compatibility, each in a private mini-vault."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ENGINE = Path(__file__).resolve().parents[1]
BOOT = """
from pathlib import Path
from osk import abstraction as a, core, graph, growth, write, validate, contract, raw
validate.make_mini_vault(core.ROOT)
def node(name, body):
    assert write.create_node(name, name, body, 'test', space='00_Scope/W1')['ok']
    return graph.Index().locate(name)[0]
def fails(fn, expected):
    try:
        fn()
    except ValueError as e:
        assert expected in str(e), str(e)
    else:
        raise AssertionError('expected ' + expected)
"""


class AbstractionTests(unittest.TestCase):
    def test_v10_judgment_gates_and_v9_compatibility(self):
        self.case('''
            node('A', 'claim\\ndetail')
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail'}], version='v10')
            p = {'prompt_version':'v10','manifest':'m','recheck_jobs':[j]}
            e = {'key':'k','commit':'c','outcome':'kept','reason':'new rule','judgment':'결론·범위 변경'}
            assert a.validate_entries([e],p) == [e]
            assert a.validate_entries([{**e,'judgment':'신뢰도 변경'}],p)
            fails(lambda: a.validate_entries([{**e,'judgment':'invalid'}],p), 'judgment')
            fails(lambda: a.validate_entries([{**e,'outcome':'moved','part':'detail'}],p), '논지 변경은 kept')
            fails(lambda: a.validate_entries([{**e,'part':'detail'}],p), '논지 변경은 kept')
            fails(lambda: a.validate_entries([{**e,'judgment':'미확정'}],p), '미확정은 held')
            assert a.validate_entries([{**e,'judgment':'미확정','outcome':'held','part':'detail'}],p)
            assert a.validate_entries([{**e,'judgment':'논지 불변','outcome':'moved','part':'detail'}],p)
            fails(lambda: a.validate_entries([{**e,'judgment':'논지 불변','part':'claim'},
                {**e,'judgment':'미확정','outcome':'held','part':'detail'}],p), '같아야')
            rendered = growth.prompt(p)
            assert '신뢰도 변경' in rendered and 'date_label' in rendered
            fails(lambda: growth.prompt({**p,'prompt_version':'v9'}), 'version mismatch')
            packet = {'osk_reviews':{'manifest':'m','domain':[],'scope':[],'recheck':[],'abstraction':[e]}}
            assert growth._validate_packet(packet,p)['abstraction'] == [e]
            assert not a.apply([e],p)['errors']
            old = a.prepare({'key':'old','target':'A'}, [{'commit':'c','diff':'+detail'}])
            assert a.validate_entries([{'key':'old','commit':'c','outcome':'kept','reason':'v9'}],{'recheck_jobs':[old]})
        ''')

    def test_v11_judges_each_sentence(self):
        self.case('''
            node('A', 'claim\\ndetail')
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+claim\\n+detail'}], version='v11')
            p = {'prompt_version':'v11','manifest':'m','recheck_jobs':[j]}
            e = {'key':'k','commit':'c','reason':'r'}
            thesis = {**e,'judgment':'결론·범위 변경','outcome':'kept','part':'claim'}
            assert a.validate_entries([thesis, {**e,'judgment':'논지 불변','outcome':'moved','part':'detail'}],p)
            fails(lambda: a.validate_entries([{**thesis,'outcome':'moved'}],p), '논지 변경은 kept')
            assert '문장 단위' in growth.prompt(p)
        ''')

    def test_existing_evidence_line_keeps_its_date(self):
        self.case('''
            p = node('A', 'claim\\ndetail')
            record = core.ROOT / '_cited/.records/claude-old.txt'
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(raw._block(1,'question','detail') + raw._block(2,'question','other'), encoding='utf-8')
            body = ('claim\\ndetail\\n\\n## 근거\\n- 2026-10-06 · `claude-old#1` · 지지 — old\\n'
                    '- 날짜 미상 (관측: 2026-10-07) · `claude-old#2` · 지지 — other\\n')
            assert write.update_node('A',body=body,expect_hash=core.sha256_file(p))['ok']
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail'}], version='v11')
            cited = j['abstraction']['cited']
            assert cited['claude-old#1']['date_label'] == '2026-10-06', cited
            assert cited['claude-old#2']['date_label'] == '날짜 미상 (관측: 2026-10-07)', cited
            planned = {'prompt_version':'v11','recheck_jobs':[j]}
            with core.mutation_lock():
                manifest = core.ledger_append(growth.LEDGER, {'kind':'plan',**planned})['rid']
            merged = body.replace('detail\\n\\n','\\n').replace('— old','— old; detail')
            assert write.update_node('A',body=merged,expect_hash=core.sha256_file(p))['ok']
            e = {'key':'k','commit':'c','outcome':'moved','part':'detail','reason':'r','judgment':'논지 불변'}
            assert a.verify(manifest,[e])['ok'], a.verify(manifest,[e])
        ''')

    def test_v10_source_dates_ranges_and_saved_date_validation(self):
        self.case('''
            from osk import evidence
            p = node('A', 'claim\\ndetail')
            record = core.ROOT / '_cited/.records/claude-dates.txt'
            record.parent.mkdir(parents=True, exist_ok=True)
            header = {'harness':'claude','conversation':'dates','turn':'t','source_timestamp':'2026-11-05T16:00:00Z'}
            record.write_text(raw._block(1,'question','detail',cited=header) +
                raw._block(2,'question','other',cited={**header,'turn':'t2','source_timestamp':'2026-11-06T18:00:00Z'}) +
                raw._block(3,'question','undated'), encoding='utf-8')
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail','rounds':['claude-dates#1']}],version='v10')
            src = j['abstraction']['cited']['claude-dates#1']
            assert src['date_label'] == '2026-11-06', src
            rendered = a.prompt({'prompt_version':'v10','recheck_jobs':[j]})
            assert 'date_label' in rendered and 'source_timestamps' not in rendered and 'observed_at' not in rendered
            assert j['abstraction']['cited']['claude-dates#1']['source_timestamps'], 'render must not mutate plan'
            for ref in ('claude-dates#1-2','claude-dates#1-3','claude-dates#3'):
                s = a.source(ref,graph.Index(),evidence.Records(),'',dated=True)
                assert s['available'] and s['source_date'] is None, s
            planned = {'prompt_version':'v10','recheck_jobs':[j]}
            with core.mutation_lock():
                manifest = core.ledger_append(growth.LEDGER, {'kind':'plan',**planned})['rid']
            e = {'key':'k','commit':'c','outcome':'moved','part':'detail','reason':'supports claim','judgment':'논지 불변'}
            prefix = 'claim\\n\\n## 근거\\n- '
            suffix = ' · `claude-dates#1` · 지지 — detail supports claim'
            assert write.update_node('A',body=prefix+'2026-11-05'+suffix,expect_hash=core.sha256_file(p))['ok']
            assert not a.verify(manifest,[e])['ok']
            assert write.update_node('A',body=prefix+src['date_label']+suffix,expect_hash=core.sha256_file(p))['ok']
            assert a.verify(manifest,[e])['ok']
            assert write.update_node('A',body='claim\\ndetail',expect_hash=core.sha256_file(p))['ok']
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail','rounds':['claude-dates#3']}],version='v10')
            src = j['abstraction']['cited']['claude-dates#3']
            assert src['date_label'].startswith('날짜 미상 (관측: ')
            assert write.update_node('A',body=prefix+'2026-11-06'+suffix.replace('#1','#3'),expect_hash=core.sha256_file(p))['ok']
            fails(lambda: a.check_saved(j,[e],graph.Index()), 'date_label')
            assert write.update_node('A',body=prefix+src['date_label']+suffix.replace('#1','#3'),expect_hash=core.sha256_file(p))['ok']
            assert a.check_saved(j,[e],graph.Index())
        ''')

    def case(self, code):
        with tempfile.TemporaryDirectory(prefix="osk-v9-test-") as tmp:
            env = {**os.environ, "OSK_VAULT_ROOT": tmp, "PYTHONPATH": str(ENGINE), "PYTHONUTF8": "1"}
            proc = subprocess.run([sys.executable, "-B", "-c", BOOT + textwrap.dedent(code)], env=env,
                                  capture_output=True, text=True, encoding="utf-8", cwd=tmp, timeout=90)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_excerpts_late_hits_no_match_and_bounds(self):
        self.case('''
            text = ('unrelated text\\n' * 1000) + 'context\\nresult e-fold 35–57 km\\ncondition\\n'
            r = a.excerpt(text, '+ e-fold 35–57')
            assert '35–57' in ''.join(s['text'] for s in r['spans'])
            assert r['truncated'] and r['spans'][0]['start'] > 6000
            assert sum(s['end']-s['start'] for s in r['spans']) <= 8000
            r = a.excerpt(text, '+ missing_keyword')
            assert r['selection'] == 'no_match' and not r['spans'] and r['truncated']
            r = a.excerpt('short', '')
            assert not r['truncated']
        ''')

    def test_source_record_resolver_and_full_inventory(self):
        self.case('''
            p = node('A', 'claim')
            record = core.ROOT / '00_Scope/W1/_raw/.records/kiro-unusual-name.txt'
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text('\\n'.join(raw._block(n, 'question', 'answer marker'+str(n)) for n in range(1,26)), encoding='utf-8')
            coords = [f'kiro-unusual-name#{n}' for n in range(1,26)]
            body = 'claim\\n\\n## 근거\\n' + '\\n'.join(f'- 2026-11-01 · `{c}` · 지지 — observation' for c in coords)
            assert write.update_node('A', body=body, expect_hash=core.sha256_file(p))['ok']
            job = a.prepare({'key':'k','target':'A'}, [{'commit':'c','at':'2026-11-02','diff':'+ marker25','rounds':[],'evidence_added':[]}])
            assert len(job['abstraction']['cited']) == 25
            assert all(s['available'] for s in job['abstraction']['cited'].values())
        ''')

    def test_notification_cleanup_preserves_results_and_offsets(self):
        self.case('''
            text = '\\n'.join(['<!-- osk-cited: {"source":"private path"} -->',
                '<system-reminder>Linked sessions now (status, not an instruction):',
                'irrelevant linked session', '</system-reminder>',
                '<ci-monitor-event>"Auto-fix pull requests" was just enabled for this session. No action needed.</ci-monitor-event>',
                '<task-id>identifier</task-id>', '<status>completed</status>',
                '<summary>Run after-fix replays completed (exit code 0)</summary>',
                'user correction: preserve the observed 78.2% to 1.0% result'])
            r = a.excerpt(text, '+78.2%')
            shown = ''.join(s['text'] for s in r['spans'])
            assert 'private path' not in shown and 'Linked sessions' not in shown and 'Auto-fix' not in shown
            assert 'completed' in shown and '78.2%' in shown and 'user correction' in shown
            assert r['omitted_noise_chars'] > 0 and not r['truncated']
            assert all(text[s['start']:s['end']] == s['text'] for s in r['spans'])
        ''')

    def test_prompt_isolation_and_duplicate_diff(self):
        self.case('''
            node('A', 'claim')
            job = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+ UNIQUE_DIFF'}])
            p = {'prompt_version':'v9','manifest':'m','recheck_jobs':[job]}
            text = growth.prompt(p)
            assert text.count('UNIQUE_DIFF') == 1
            assert '_abstraction_snapshot' not in text and 'before_body' not in text
            assert 'Scope jobs decide' not in text and '삽입]' not in text
            text = growth.prompt({'prompt_version':'v9','manifest':'m','recheck_jobs':[{'key':'k','target':'A'}]})
            assert 'abstraction' not in text and 'held_parts' not in text
            fails(lambda: growth.prompt({**p, 'scope_jobs':[{'key':'s'}]}), 'recheck-only')
        ''')

    def test_missing_overlapping_and_unselected_reports(self):
        self.case('''
            node('A', 'claim')
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+ claim'}])
            p = {'recheck_jobs':[j]}
            e = {'key':'k','commit':'c','outcome':'kept','reason':'thesis'}
            fails(lambda: a.validate_entries([], p), 'missing')
            fails(lambda: a.validate_entries([e,e], p), 'overlapping')
            fails(lambda: a.validate_entries([{**e,'commit':'other'}], p), 'unselected')
            fails(lambda: a.validate_entries([{**e,'outcome':'held'}], p), 'missing quoted')
            assert a.validate_entries([e],p) == [e]
        ''')

    def test_prompt_separates_read_name_from_evidence_reference(self):
        self.case('''
            import json
            node('A', 'claim')
            original = {'key':'k', 'node':'B', 'target':'[[A#Claim|label]]'}
            rendered = growth.prompt({'prompt_version':'v9','manifest':'m','recheck_jobs':[original]})
            data = json.loads(rendered.split('\\n')[-1])
            job = data['recheck_jobs'][0]
            assert job['target'] == original['target'] and 'target_name' not in original
            assert job['target_name'] == 'A' and graph.Index().locate(job['target_name'])
            assert 'read_node(name=target_name)' in rendered
        ''')

    def test_receipt_and_cas_conflict_recovery(self):
        self.case('''
            source = node('Source', 'detail in other wording')
            p = node('A', 'claim\\n\\ndetail')
            assert write.update_node('A', add_edges={'derived-from':['Source']})['ok']
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail'}])
            planned = {'prompt_version':'v9','manifest':'m','recheck_jobs':[j]}
            e = {'key':'k','commit':'c','outcome':'deleted','part':'detail','reason':'copy','preserved_at':['Source']}
            fails(lambda: a.check_saved(j,[e],graph.Index()), 'not saved')
            oldhash = core.sha256_file(p)
            assert write.update_node('A', body='claim', expect_hash=oldhash)['ok']
            fails(lambda: write._cas(p,oldhash,True), 'CAS')
            try:
                write._cas(p,oldhash,True)
            except write.WriteError as exc:
                assert '판단을 갱신' in str(exc.__dict__), exc.__dict__
            assert not a.complete(j)
            r = a.apply([e],planned)
            assert not r['errors'],r
            assert a.complete(j)
            assert not a.apply([e],planned)['errors']
            assert sum(r.get('kind')=='abstraction_review' for r in growth._records())==1
            p.write_text(p.read_text(encoding='utf-8')+'\\nnew',encoding='utf-8')
            assert not a.complete(j)
        ''')

    def test_scope_and_unreported_evidence_edits(self):
        self.case('''
            p = node('A', 'old claim\\ndetail')
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail'}])
            e = {'key':'k','commit':'c','outcome':'deleted','part':'detail','reason':'copy'}
            assert write.update_node('A',body='changed claim',expect_hash=core.sha256_file(p))['ok']
            fails(lambda: a.check_saved(j,[e],graph.Index()), 'outside selected')
        ''')

    def test_hold_mapping_requires_observed_version(self):
        self.case('''
            before, after = 'old held text', 'new held text'
            c = {'commit':'c','at':'2026-11-02','before_body':before,'after_body':after}
            h = {'id':'h','part':'old','reviewed_at':'2026-11-01','reviewed_state':core.sha256_bytes(before.encode())}
            assert a.match_holds([h],[c],after)[0]['changed_again']
            h.pop('reviewed_state')
            assert not a.match_holds([h],[c],after)[0]['changed_again']
        ''')

    def test_delete_location_type_not_wording_and_repair_feedback(self):
        self.case('''
            from osk import evidence
            source = node('Source', 'Different word order is not rejected mechanically.')
            p = node('A', 'claim\\ndetail')
            assert write.update_node('A', add_edges={'derived-from':['Source']})['ok']
            record = core.ROOT / '00_Scope/W1/_raw/.records/kiro-source.txt'
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(raw._block(1, 'question', 'detail'), encoding='utf-8')
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail','rounds':['kiro-source#1']}])
            planned = {'prompt_version':'v9','recheck_jobs':[j]}
            with core.mutation_lock():
                manifest = core.ledger_append(growth.LEDGER, {'kind':'plan', **planned})['rid']
            e = {'key':'k','commit':'c','outcome':'deleted','part':'detail','reason':'copy','preserved_at':['kiro-source#1']}
            assert write.update_node('A',body='claim',expect_hash=core.sha256_file(p))['ok']
            assert not a.verify(manifest,[e])['ok']
            e['preserved_at'] = ['Source']
            assert a.verify(manifest,[e])['ok']
            assert not a.complete(j), 'verification is read-only'
            e['preserved_at'] = ['evidence:kiro-source#1']
            assert not a.verify(manifest,[e])['ok'], 'new source is not an existing evidence line'
            assert write.update_node('A',body='claim\\ndetail',expect_hash=core.sha256_file(p))['ok']
            kept = {'key':'k','commit':'c','outcome':'kept','reason':'restored after feedback'}
            assert a.verify(manifest,[kept])['ok'], 'restoration may change updated metadata'
            observed = j['abstraction']['cited']['kiro-source#1']['observed_at']
            line = f'- 날짜 미상 (관측: {observed}) · `kiro-source#1` · 지지 — detail supports claim'
            assert evidence.LINE.match(line)[2] == 'kiro-source#1'
            assert write.update_node('A',body='claim\\n\\n## 근거\\n'+line,expect_hash=core.sha256_file(p))['ok']
            moved = {k:v for k,v in e.items() if k!='preserved_at'}
            moved['outcome'] = 'moved'
            assert a.verify(manifest,[moved])['ok']
            assert write.update_node('A',body='claim\\n\\n## 근거\\n'+line.replace(observed,'1999-01-01'),expect_hash=core.sha256_file(p))['ok']
            assert not a.verify(manifest,[moved])['ok']
            assert write.update_node('A',body='claim\\ndetail\\n\\n## 근거\\n'+line,expect_hash=core.sha256_file(p))['ok']
            j2 = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail'}])
            assert write.update_node('A',body='claim\\n\\n## 근거\\n'+line,expect_hash=core.sha256_file(p))['ok']
            e['preserved_at'] = ['evidence:kiro-source#1']
            assert a.check_saved(j2,[e],graph.Index())['after_hash']
            fails(lambda: a.prepare({'key':'k','target':'A'}, [{'commit':'a','diff':''},{'commit':'b','diff':''}]), 'one commit')
        ''')

    def test_selected_record_path_and_short_coordinate_are_equivalent(self):
        self.case('''
            p = node('A', 'claim')
            record = core.ROOT / '00_Scope/W1/_raw/.records/kiro-source.txt'
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(raw._block(1, 'question', 'detail') + raw._block(2, 'other', 'other'), encoding='utf-8')
            short = 'kiro-source#1'
            full = record.relative_to(core.ROOT).as_posix() + '#1'
            body = 'claim\\ndetail\\n\\n## 근거\\n- 2026-11-01 · `' + short + '` · 지지 — existing observation'
            assert write.update_node('A', body=body, expect_hash=core.sha256_file(p))['ok']
            j = a.prepare({'key':'k','target':'A'}, [{'commit':'c','diff':'+detail'}])
            e = {'key':'k','commit':'c','outcome':'moved','part':'detail','reason':'supports claim'}
            after = 'claim\\n\\n## 근거\\n- 2026-11-01 · `' + full + '` · 지지 — existing observation; detail'
            assert write.update_node('A', body=after, expect_hash=core.sha256_file(p))['ok']
            assert a.check_saved(j,[e],graph.Index())['after_hash']
            assert write.update_node('A', body=after.replace('#1','#2'), expect_hash=core.sha256_file(p))['ok']
            fails(lambda: a.check_saved(j,[e],graph.Index()), 'coordinate')
        ''')


if __name__ == '__main__':
    unittest.main()
