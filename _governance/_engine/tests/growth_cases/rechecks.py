"""성장 실행의 수트의 한 갈래: 근거 재검토와 근거 절 — 큐, 올림과 보류, 이행.

`tests/test_growth.py`의 `GrowthTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""

from growth_cases.support import UPGRADE_CARRY, UPGRADE_NO_PAIR


class RecheckCases:
    def test_recheck_jobs_queue_in_daily_runs_and_fork_scope(self):
        self.check_case("""
            node('A')
            assert write.create_node(VAULT, 'B', 'b', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            assert not growth_plan.plan(VAULT, 3)['recheck_jobs']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            planned = growth_plan.plan(VAULT, 3)
            jobs = planned['recheck_jobs']
            assert [(j['node'], j['target']) for j in jobs] == [('B', '[[A]]')], jobs
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'pending'
            assert 'For recheck_jobs' in growth.prompt(VAULT, planned)
            assert [j['node'] for j in growth_plan._recheck_jobs(VAULT, graph.Index(VAULT), 'W1')] == ['B']
            assert not growth_plan._recheck_jobs(VAULT, graph.Index(VAULT), 'W2')   # a fork keeps to its scope
            assert not growth_ledger.daily_active(VAULT)
            manifest = register(planned)
            assert not growth_ledger.daily_active(VAULT), 'a plan is not completed work'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':manifest['rid'],'ok':True})
            assert growth_ledger.daily_active(VAULT)
            write.update_node(VAULT, 'B', add_edges={'derived-from': 'A'})
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'pending', 'wiring is not a check'
            write.update_node(VAULT, 'B', rechecked=['A'])
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'complete'
            assert not growth_plan.plan(VAULT, 3)['recheck_jobs']
        """)

    def test_latest_daily_result_controls_recheck_handoff(self):
        self.check_case("""
            from datetime import datetime, timedelta, timezone
            from unittest.mock import patch
            planned = register()
            assert not growth_ledger.daily_active(VAULT)
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':planned['rid'],'ok':True})
            assert growth_ledger.daily_active(VAULT)
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'plan','work_context':'stop:W1'})
            assert growth_ledger.daily_active(VAULT), 'Stop work must not replace daily health'
            latest = register()
            assert not growth_ledger.daily_active(VAULT), 'a newer unfinished plan supersedes old success'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':latest['rid'],'ok':False})
            assert not growth_ledger.daily_active(VAULT), 'failed daily work must yield rechecks to forks'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':latest['rid'],'ok':True})
            assert growth_ledger.daily_active(VAULT)
            rows = growth_ledger.ledger_records(VAULT)
            old = (datetime.now(timezone.utc)-timedelta(days=4)).isoformat()
            for row in rows:
                row['at'] = old
            with patch.object(growth_ledger,'ledger_records',return_value=rows):
                assert not growth_ledger.daily_active(VAULT), 'expired success does not suppress fallback'
        """)

    def test_daily_handoff_follows_the_days_recheck_queue(self):
        self.check_case("""
            # #301 / #306 D7: the fork takes rechecks over only when that day's recheck queue was not done —
            # no light batch got its turn, or one left a selected recheck neither closed nor deferred.
            from datetime import datetime, timezone
            day = datetime.now(timezone.utc).astimezone().date().isoformat()
            def batch(kind, run=None, rechecks=(), waiting=0, **daily):
                plan = core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind': 'plan', 'work_context': 'daily',
                                                          'daily': {'day': day, 'kind': kind, **daily},
                                                          'recheck_jobs': [{'key': k} for k in rechecks],
                                                          'queued_not_selected': {'recheck_jobs': waiting}})
                if run is not None:
                    core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind': 'run', 'manifest': plan['rid'], 'returncode': 0, **run})
                return plan
            def fresh():
                growth_ledger.ledger_path(VAULT).unlink(missing_ok=True)
            fresh()
            batch('scope', {'state': 'incomplete', 'ok': False, 'returncode': 1})
            assert not growth_ledger.daily_active(VAULT), 'the day stopped before any recheck batch'
            batch('light', {'state': 'incomplete', 'ok': False,
                            'recheck_outcomes': {'r1': {'status': 'complete'}}}, ['r1'])
            assert growth_ledger.daily_active(VAULT), 'a failed Scope batch and leftover organization do not hand rechecks over'
            batch('light', {'state': 'limited', 'ok': False}, ['r2'])
            assert growth_ledger.daily_active(VAULT), 'a limited batch is a wait — another provider ran it again'
            plan = batch('light', {'state': 'incomplete', 'ok': False,
                                   'recheck_outcomes': {'r3': {'status': 'pending'}}}, ['r3'])
            assert not growth_ledger.daily_active(VAULT), 'a selected recheck left open'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind': 'recheck_review', 'manifest': plan['rid'], 'key': 'r3',
                                               'outcome': 'deferred', 'reason': 'cannot judge yet'})
            assert growth_ledger.daily_active(VAULT), 'a deferral is a judgement'
            batch('light', {'state': 'incomplete', 'ok': False, 'returncode': 1,
                            'recheck_outcomes': {'r4': {'status': 'complete'}}}, ['r4'])
            assert not growth_ledger.daily_active(VAULT), 'a failed worker judged nothing'
            fresh()
            batch('light')
            assert growth_ledger.daily_status(VAULT)['ran'] and not growth_ledger.daily_active(VAULT), 'a plan without a result'
            # #312 review: other light work took the places, so no recheck was picked though some waited.
            fresh()
            batch('light', {'state': 'complete', 'ok': True}, waiting=2)
            assert not growth_ledger.daily_active(VAULT), 'rechecks waited while other light work took the places'
            batch('light', {'state': 'incomplete', 'ok': False,
                            'recheck_outcomes': {'r5': {'status': 'complete'}}}, ['r5'], waiting=1)
            assert growth_ledger.daily_active(VAULT), 'a later batch of the day gave the rechecks their turn'
            # With no candidate waiting, a light batch without rechecks leaves nothing to hand over.
            fresh()
            batch('light', {'state': 'complete', 'ok': True})
            assert growth_ledger.daily_active(VAULT)
            # A backlog run is not the daily run: its Scope batches neither replace nor date the day.
            batch('scope', {'state': 'incomplete', 'ok': False}, backlog=True)
            assert growth_ledger.daily_active(VAULT) and growth_ledger.daily_status(VAULT)['last']['state'] == 'complete'
        """)

    def test_recheck_note_tells_an_unfinished_daily_run_from_a_missing_one(self):
        self.check_case("""
            from unittest.mock import patch
            sys.path.insert(0, str(Path(growth.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            class Rechecks:
                @staticmethod
                def candidates(vault):
                    return [{'node': 'B', 'target': '[[A]]'}], False
            ran = {'ran': True, 'active': False,
                   'last': {'at': '2026-10-09T09:00:02+09:00', 'state': 'incomplete'}}
            with patch.object(growth_ledger, 'daily_status', return_value=ran):
                text = hook._recheck_note(VAULT, Rechecks)
            assert '최신 정기 실행(' in text and '다 끝내지 못해' in text, text
            assert '돌지 않아' not in text and 'SETUP' not in text, text
            with patch.object(growth_ledger, 'daily_status', return_value={'ran': False, 'active': False, 'last': None}):
                text = hook._recheck_note(VAULT, Rechecks)
            assert '돌지 않아' in text and 'SETUP' in text, text
        """)

    def test_recheck_escalation_holds_a_second_correction_for_the_user(self):
        self.check_case("""
            from osk import rechecks
            node('A')
            for title, body, basis in (('B', 'B relies on A.', 'A'), ('C', 'C relies on B.', 'B')):
                assert write.create_node(VAULT, title, title, body, 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': basis})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            jobs = growth_plan.plan(VAULT, 3)['recheck_jobs']
            assert [(j['node'], j['cascade'], j['next']) for j in jobs] == [('B', False, ['C'])], jobs
            # the agent's own correction of B is autonomous; C's recheck is then a cascade
            write.update_node(VAULT, 'B', old_text='B relies on A.', new_text='B relies on revised A.',
                              rechecked=['A'])
            planned = growth_plan.plan(VAULT, 3)
            jobs = planned['recheck_jobs']
            assert [(j['node'], j['cascade']) for j in jobs] == [('C', True)], jobs
            manifest = register({**planned, 'scope_jobs': []})   # as run() records it
            packet = {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                      'recheck': [{'key': jobs[0]['key'], 'outcome': 'unchanged',
                                   'reason': 'x', 'proposal': 'y'}]}}
            refused = growth_reviews.checkpoint(VAULT, packet)
            assert not refused['ok'] and any('only escalates' in e for e in refused['errors']), refused
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] != 'complete', 'a recheck review closed a check'
            packet['osk_reviews']['recheck'][0]['outcome'] = 'escalated'
            assert growth_reviews.checkpoint(VAULT, packet)['ok']
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'complete'
            assert not growth_plan.plan(VAULT, 3)['recheck_jobs'], 'an escalated check returned to the agent queue'
            held = rechecks.report(VAULT, graph.Index(VAULT)).get('escalated')
            assert [h['node'] for h in held] == ['C'] and held[0]['proposal'] == 'y', held
            write.update_node(VAULT, 'C', rechecked=['B'])   # the user's decision
            assert not rechecks.report(VAULT, graph.Index(VAULT)), rechecks.report(VAULT, graph.Index(VAULT))
        """)

    def test_recheck_deferral_keeps_the_pair_open_and_hands_its_reason_on(self):
        # Bylaws §7 2: a recheck the agent could not judge is not closed; its reason goes to the
        # next recheck of the same node and target.
        self.check_case("""
            from osk import rechecks
            node('A')
            assert write.create_node(VAULT, 'B', 'B', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            planned = growth_plan.plan(VAULT, 3)
            job = planned['recheck_jobs'][0]
            assert 'previous_deferral' not in job, job
            manifest = register({**planned, 'scope_jobs': []})
            done = growth_reviews.checkpoint(VAULT, {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                'recheck': [{'key': job['key'], 'outcome': 'deferred',
                             'reason': 'read both; the revision may narrow B', 'proposal': 'drop me'}]}})
            assert done['ok'] and any('carries no proposal' in n for n in done['notes']), done
            row = [r for r in growth_ledger.ledger_records(VAULT) if r.get('kind') == 'recheck_review'][-1]
            assert row['outcome'] == 'deferred' and 'proposal' not in row, row
            assert growth_plan.recheck_status(VAULT, job, graph.Index(VAULT))['status'] == 'pending', 'a deferral closed the check'
            assert not rechecks.report(VAULT, graph.Index(VAULT)).get('escalated'), 'a deferral reached the user'
            again = growth_plan.plan(VAULT, 3)['recheck_jobs']
            assert [j['key'] for j in again] == [job['key']], again
            handed = again[0]['previous_deferral']
            assert handed['reason'].startswith('read both') and handed['state_changed'] is False, handed
            write.update_node(VAULT, 'A', old_text='A revised observation', new_text='A twice revised observation')
            planned = growth_plan.plan(VAULT, 3)
            assert planned['recheck_jobs'][0]['previous_deferral']['state_changed'] is True, planned['recheck_jobs']
            # an escalation still carries its proposal
            manifest = register({**planned, 'scope_jobs': []})
            refused = growth_reviews.checkpoint(VAULT, {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                'recheck': [{'key': planned['recheck_jobs'][0]['key'], 'outcome': 'escalated', 'reason': 'x'}]}})
            assert not refused['ok'] and any('invalid recheck review fields' in e for e in refused['errors']), refused
        """)

    def test_a_run_whose_only_open_jobs_are_rechecks_applies_its_final_packet(self):
        # The runner applies the worker's final packet while a selected job is open. A run
        # left with rechecks alone must apply its escalations and deferrals too (#162 review).
        self.check_case("""
            node('A')
            assert write.create_node(VAULT, 'B', 'B', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            job = growth_plan.plan(VAULT, 3)['recheck_jobs'][0]
            only = {'candidates': [], 'scope_jobs': [], 'organization_jobs': [], 'eviction_jobs': [],
                    'recheck_jobs': [job]}
            assert growth_reviews.needs_review(VAULT, only, growth_ledger.ledger_records(VAULT), graph.Index(VAULT)), 'an open recheck did not ask for the packet'
            write.update_node(VAULT, 'B', rechecked=['A'])
            assert not growth_reviews.needs_review(VAULT, only, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
        """)

    def test_evidence_section_changes_do_not_propagate_and_old_records_carry_over(self):
        # Bylaws §7 2: the 근거 section at a node's end lists its non-node evidence, so changing
        # it is not a related-state change. A record measured before that rule carries over.
        self.check_case("""
            from osk import rechecks
            first = '- 2026-10-05 · `c38d8ef2#1` · 지지 — first'
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '', first]))
            assert write.create_node(VAULT, 'B', 'B', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0]
            write.update_node(VAULT, 'A', old_text=first, new_text=first + chr(10) + '- 2026-10-06 · `c38d8ef2#2` · 조건 — second')
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'a 근거 line made B a recheck candidate'
            # only the node's own heading starts it: a quoted or listed `## 근거` is body
            for inner in ('> ## 근거' + chr(10) + '> quoted line', '- item' + chr(10) + chr(10) + '  ## 근거' + chr(10) + '  listed'):
                assert rechecks.evidence_span('A claim.' + chr(10) + chr(10) + inner) is None, inner
            # a record that measured the whole body, the 근거 section included
            idx = graph.Index(VAULT)
            a, b = (idx.locate(n)[0] for n in ('A', 'B'))
            data = a.read_bytes()
            whole = core.sha256_bytes(data[contract.body_offset(data):])
            assert whole != rechecks.state(data)
            core.ledger_append(rechecks.ledger_path(VAULT), rechecks._row(idx.node(b).id, rechecks.state(b.read_bytes()),
                                                                  idx.node(a).id, whole, 'unchanged'))
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'an unchanged old-definition record stopped holding'
            assert rechecks.ensure_baseline(VAULT) == 0, 'a node write would scan every pair'
            assert rechecks.ensure_baseline(VAULT, carry=True) == 1
            row = core.ledger_read(rechecks.ledger_path(VAULT))[-1]
            assert row['reason'] == '이어받음' and row['target_state'] == rechecks.state(data), row
            write.update_node(VAULT, 'A', old_text='— second', new_text='— second, again')
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'the carried record did not hold'
            write.update_node(VAULT, 'A', old_text='A claim.', new_text='A revised claim.')
            assert [i['node'] for i in rechecks.candidates(VAULT, graph.Index(VAULT))[0]] == ['B']
        """)

    def test_upgrade_keeps_escalations_and_cascades_recorded_before_the_evidence_rule(self):
        # Records written before the 근거 section left the state measured it. While the content
        # holds, a pending user review and a cascade must survive the upgrade (another session
        # reproduced both being lost on 2026-10-07), and an edit of a 근거 section after the carry
        # must not drop them (#177).
        self.check_case(UPGRADE_CARRY.replace('V50_CARRY', 'False'))

    def test_a_recheck_made_version_carries_when_no_pair_is_complete(self):
        self.check_case(UPGRADE_NO_PAIR)

    def test_a_v5_0_carry_still_keeps_the_cascade_after_an_evidence_edit(self):
        # v5.0.x carried the completion as `unchanged` and left the user review as it was. While
        # the content holds, the next carry still marks the version a recheck made (#177).
        self.check_case(UPGRADE_CARRY.replace('V50_CARRY', 'True'))

    def test_evidence_section_rule_resolves_coordinates_and_reports_until_activated(self):
        # Mechanism §6-1 4: each 근거 line has the §8 9 form and a coordinate that resolves — a
        # record and its rounds, a vault non-node file and its heading, a URL. A node is not a
        # 근거 line, and a non-node left in derived-from is reported until it moves to the section.
        self.check_case("""
            from osk import evidence, raw
            claude = 'claude-c38d8ef2' + '0' * 24
            first = raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': 'q1', 'agent': 'a1'},
                                                          {'user': 'q2', 'agent': 'a2'}], '00_Scope/W1')
            (VAULT.root / '_sources' / 'r.md').write_text('# 결과' + chr(10) + 'x' + chr(10), encoding='utf-8')
            (VAULT.root / '_sources' / 'd').mkdir()
            node('N')
            lines = ['- 2026-10-05 · `c38d8ef2#1–2` · 지지 — rounds',
                     '- 2026-10-05 · `c38d8ef2#3` · 반례 — no round',
                     '- 2026-10-05 · `_sources/r.md#결과` · 조건 — file',
                     '- 2026-10-05 · `_sources/r.md#없다` · 조건 — heading',
                     '- 2026-10-05 · `_sources/d` · 조건 — directory',
                     '- 2026-10-05 · `https://example.com/a` · 지지 — url',
                     '- 2026-10-05 · `N` · 지지 — node',
                     '- 2026-10-05 · `brain:wiki/x.md` · 지지 — alias',
                     '- 10-05 · c38d8ef2#1 · 지지 — form']
            text = chr(10).join(['x', '', '## 근거', ''] + lines)
            idx = graph.Index(VAULT)
            errs = validate.evidence_errors(VAULT, text, [], idx)
            assert errs == ['없는 라운드: c38d8ef2#3', '없는 제목: _sources/r.md#없다', '없는 파일: _sources/d',
                            '노드를 가리키는 근거 줄: N — 노드 근거는 derived-from으로 옮긴다',
                            '선언되지 않은 별칭: brain:wiki/x.md',
                            '형식이 다른 관측 줄: - 10-05 · c38d8ef2#1 · 지지 — form'], errs
            # a shortened hash two records share is ambiguous; a longer prefix tells them apart
            raw.append_rounds(VAULT, 'repo/ev', 'codex-c38d8ef2' + '1' * 24, [{'user': 'q', 'agent': 'a'}], '00_Scope/W1')
            records = raw.Records(VAULT)
            assert validate.evidence_errors(VAULT, text, [], idx, records)[0].startswith('모호한 좌표: c38d8ef2#1–2')
            assert records.short(claude) == 'c38d8ef20', records.short(claude)
            longer = chr(10).join(['x', '', '## 근거', '', '- 2026-10-05 · `c38d8ef20#1–2` · 지지 — longer'])
            assert validate.evidence_errors(VAULT, longer, [], idx, records) == []
            # `# 관측` opens the section as the pre-v5.6 `## 근거` does (Mechanism §4-1 1), and renaming
            # the heading leaves the relevant state as it was — it is measured up to the heading line
            from osk import rechecks
            observed = longer.replace('## 근거', '# 관측')
            assert validate.evidence_errors(VAULT, observed, [], idx, records) == []
            assert validate.evidence_errors(VAULT, observed + chr(10) + '## 이력' + chr(10), []) == ['관측 절이 노드 끝에 있지 않다']
            assert rechecks.evidence_span('x' + chr(10) + '## 관측' + chr(10) + '- a') is None
            assert rechecks.evidence_span('x' + chr(10) + '# 근거' + chr(10) + '- a') is None
            fm = '---' + chr(10) + 'id: x' + chr(10) + '---' + chr(10)
            assert rechecks.state((fm + longer).encode()) == rechecks.state((fm + observed).encode())
            # a node named 관측 whose body opens with its own `# 관측` title: the claim is not a section,
            # so editing it changes the state and still reaches the nodes that derive from it (#310 review)
            titled = '# 관측' + chr(10) + chr(10) + '주장 A.' + chr(10)
            assert rechecks.evidence_span(titled) is None
            assert validate.evidence_errors(VAULT, titled, []) == []
            assert rechecks.state((fm + titled).encode()) != rechecks.state((fm + titled.replace('A', 'B')).encode())
            # a `# 관측` section added after a legacy `## 근거` one turns the older lines into body text —
            # the validator reports the stranded heading (review of #311)
            stranded = longer + chr(10) + chr(10) + '# 관측' + chr(10) + chr(10) + '- 2026-11-09 · `c38d8ef20#1` · 지지 — new'
            assert validate.evidence_errors(VAULT, stranded, [], idx, records) == ['관측 절이 노드 끝에 있지 않다']
            # a non-node left in derived-from is reported once; a node there is not
            assert validate.evidence_errors(VAULT, 'x', ['N', first['round_refs'][0]], idx) == [
                'derived-from에 비노드 대상 1개가 남아 있다 — 관측 절로 옮긴다']
            moved = chr(10).join(['x', '', '## 근거', '', '- a', '', '## 이력', '', 'y'])
            assert validate.evidence_errors(VAULT, moved, []) == ['관측 절이 노드 끝에 있지 않다']
            # any list marker is a list item
            starred = chr(10).join(['x', '', '## 근거', '', '* 2026-10-05 · `c38d8ef20#1` · 지지 — star'])
            assert validate.evidence_errors(VAULT, starred, [])[0].startswith('형식이 다른 관측 줄: * 2026'), 'a * item was skipped'
            nested = chr(10).join(['x', '', '## 근거', '', '- 2026-10-05 · `c38d8ef20#1` · 지지 — fine',
                                   '    * 2026-10-05 · `c38d8ef20#1` · 지지 — nested', 'a paragraph'])
            errs = validate.evidence_errors(VAULT, nested, [])
            assert [e.split(':')[0] for e in errs] == ['형식이 다른 관측 줄'] * 2, errs
            quoted = chr(10).join(['x', '', '> ## 근거', '>', '> - a'])
            assert validate.evidence_errors(VAULT, quoted, []) == [], 'a quoted heading was read as the section'
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '', '- 2026-10-05 · `deadbeef#2` · 지지 — none',
                                         '- 2026-10-05 · `brain:wiki/x.md` · 지지 — held']))
            # B was written before v5.4 with a round in derived-from; writes no longer add one
            from unittest import mock
            assert 'derived-from은 노드만' in str(node_checks.check_edges(VAULT, {'derived-from': first['round_refs'][0]}, idx))
            with mock.patch.object(node_checks, 'check_edges', return_value=[]):
                assert write.create_node(VAULT, 'B', 'B', 'B claim.', 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': first['round_refs'][0]})['ok']
            from osk import roots
            roots.declare(VAULT, 'brain', 'wiki')
            rep = validate.run(VAULT, cli.published_tools)
            # a non-node left in derived-from is counted by node, not listed line by line
            assert list(rep['evidence_section']) == ['A'] and rep['evidence_section_leftovers'] == 1, rep['evidence_section']
            # a declared root that is not on this device is held, not a violation (Mechanism §6-1 4)
            assert rep['evidence_section']['A'] == ['없는 기록: deadbeef#2'], rep['evidence_section']
            assert rep['evidence_section_held'] == {'brain': '이 기기에 위치가 없다'}, rep['evidence_section_held']
            assert not rep['evidence_section_active']
            assert not any('근거 절' in f for d in rep['fail'] for f in d), rep['fail']
            core.ledger_append(VAULT.validators, {'kind': 'activate', 'rule': 'evidence-section'})
            assert any('근거 절' in f for d in validate.run(VAULT, cli.published_tools)['fail'] for f in d), 'an active rule did not count'
        """)

    def test_a_clean_rule_without_records_turns_itself_on_once_a_day(self):
        # Bylaws §11(3), Mechanism §6-1(2): a rule with violations stays report-only; once clean it is
        # activated at the next daily check, and a rule the user turned off is never turned back on.
        self.check_case("""
            from osk import response_growth
            day = 24 * 3600
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '', '- not a line']))
            assert 'evidence-section' not in validate.auto_activate(VAULT, now=day)
            assert not validate.validator_active(VAULT, 'evidence-section')
            assert write.update_node(VAULT, 'A', old_text='- not a line',
                                     new_text='- 2026-11-01 · `https://example.com/a` · 지지 — a page')['ok']
            assert validate.auto_activate(VAULT, now=day + 60) == [], 'checked twice in a day'
            assert 'evidence-section' in validate.auto_activate(VAULT, now=2 * day + 60)
            assert validate.validator_active(VAULT, 'evidence-section')
            assert core.ledger_read(VAULT.validators)[-1]['reason'] == validate.AUTO_REASON
            core.ledger_append(VAULT.validators, {'kind': 'deactivate', 'rule': 'evidence-section'})
            assert validate.auto_activate(VAULT, now=9 * day) == [] and not validate.validator_active(VAULT, 'evidence-section')
            # the hooks' background worker is where it runs on every install
            import contextlib
            from unittest import mock
            from osk import conversation_capture, conversation_state
            conversation_state.state_path(VAULT, 'claude', 's').parent.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(conversation_capture, 'hook_source', return_value=('claude', 's', None)), \\
                 mock.patch.object(conversation_capture, 'capture_due', return_value=False), \\
                 mock.patch.object(conversation_state, 'state_lock', lambda *a: contextlib.nullcontext(None)), \\
                 mock.patch.object(conversation_state, 'load_state', lambda *a: {}), \\
                 mock.patch.object(response_growth, '_probe_due', return_value=False), \\
                 mock.patch.object(validate, 'auto_activate') as auto:
                response_growth.after_hook(VAULT, {'session': 'k'})
            assert auto.called
        """)

    def test_a_user_deactivation_that_lands_during_the_check_is_kept(self):
        # Mechanism §6-1(2): the engine never overturns a user's record — the CLI does not take the
        # mutation lock, so the ledger lock (`expect`) is where an activation backs off.
        self.check_case("""
            from unittest import mock
            real = core.ledger_append
            def user_first(path, record, expect=None):
                # the user's `--deactivate` lands right before the activation takes the ledger lock
                if record.get('rule') == 'evidence-section':
                    real(VAULT.validators, {'kind': 'deactivate', 'rule': 'evidence-section'})
                return real(path, record, expect)
            with mock.patch.object(validate, 'ledger_append', user_first):
                assert 'evidence-section' not in validate.auto_activate(VAULT, now=24 * 3600)
            assert not validate.validator_active(VAULT, 'evidence-section')
            assert [r['kind'] for r in core.ledger_read(VAULT.validators) if r['rule'] == 'evidence-section'] == ['deactivate']
        """)

    def test_an_anchor_written_before_tag_spacing_still_matches(self):
        # #292: writes space `#123` off a following letter (Obsidian tag defence), so the text an agent
        # just wrote is not what is stored. An anchor copied from what the agent wrote still matches.
        self.check_case("""
            from osk import contract, scope_memory as sm
            node('A', body='이슈 #275와 #12-alpha를 고친다.')
            path = next((VAULT.root / '00_Scope/W1').glob('A.md'))
            assert '#275 와 #12 -alpha' in contract.parse(path).body, contract.parse(path).body
            r = write.update_node(VAULT, 'A', old_text='#275와 #12-alpha', new_text='#275와 #12-beta')
            assert r['ok'] and '#275 와 #12 -beta' in contract.parse(path).body, r
            h = sm.read(VAULT, 'p', space='00_Scope/W1')['hash']
            assert sm.replace(VAULT, 'p', text='# W1' + chr(10) + '- 이슈 #275와 연결', expect_hash=h, space='00_Scope/W1')['ok']
            r = sm.replace(VAULT, 'p', edits=[{'old_text': '#275와 연결', 'new_text': '#275와 이음'}], space='00_Scope/W1')
            assert r['ok'] and '#275 와 이음' in sm.read(VAULT, 'p', space='00_Scope/W1')['text'], r
        """)

    def test_a_scope_node_may_cite_a_ledger_record(self):
        # Constitution 8(3): a Scope node may reference any non-node. The ledger sits under Workbench but
        # is an institutional record (Workbench contract 2.3), not work state, so it is no cross-scope reference.
        self.check_case("""
            core.ledger_append(VAULT.validators, {'kind': 'activate', 'rule': 'evidence-section'})
            ledger = VAULT.validators.relative_to(VAULT.root).as_posix()
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '',
                                         '- 2026-11-01 · `' + ledger + '` · 지지 — 활성화 기록']))
            rep = validate.run(VAULT, cli.published_tools)
            assert not any('scope 간' in f for d in rep['fail'] for v in d.values() for f in v), rep['fail']
            assert 'A' not in rep.get('evidence_section', {}), rep.get('evidence_section')
        """)

    def test_evidence_migration_moves_non_node_derived_from_into_support_lines(self):
        # Constitution 8(5·6)·14(8·9), Mechanism §5: each non-node derived-from entry becomes a `지지` line at
        # the node end, dated by the commit that brought the round (or put the entry in the frontmatter — not
        # the earlier body mention) and showing the cite note, else the user's words. An uncommitted round has
        # no date and stays. A covered entry only leaves the frontmatter and a node entry stays. The related
        # state does not change, each node is logged before it is written, and a rerun has nothing.
        self.check_case("""
            import os, subprocess
            from osk import evidence, evidence_migration as em, raw, rechecks
            def git(*a, when=None):
                env = dict(os.environ, **({'GIT_AUTHOR_DATE': when, 'GIT_COMMITTER_DATE': when} if when else {}))
                return subprocess.run(['git', '-C', str(VAULT.root), *a], check=True, capture_output=True,
                                      text=True, env=env).stdout.strip()
            git('init', '-q'); git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
            claude = 'claude-c38d8ef2' + '0' * 24
            rounds = raw.append_rounds(VAULT, 'repo/ev', claude, [
                {'user': '<system-reminder>알림</system-reminder>' + '첫 질문입니다 ' * 9, 'agent': '긴 답'},
                {'user': '둘째 질문', 'agent': '답'}], '00_Scope/W1')['round_refs']
            noted = raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': '셋째 질문', 'agent': '인용 요지 #272의 결정'}],
                                      cited={'harness': 'claude', 'conversation': 'c1', 'turn': 't1'})['round_refs'][0]
            notice = raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': '<task-notification><task-id>b1</task-id>'
                                       '</task-notification>', 'agent': '작업이 끝나 결과를 정리했다'}])['round_refs'][0]
            (VAULT.root / '_sources').mkdir(exist_ok=True)
            (VAULT.root / '_sources' / 'r.md').write_text('# 결과' + chr(10) + 'x' + chr(10), encoding='utf-8')
            section = chr(10).join(['', '', '## 근거', '', '- 2026-10-05 · `c38d8ef2#2` · 지지 — 이미 있다'])
            url = 'https://example.com/docs/page.md'
            node('N')
            node('A', body='주장 A. 출처는 ' + url)
            node('B', body='주장 B.' + section)
            node('C', body='주장 C.' + section + chr(10) + chr(10) + '## 이력' + chr(10) + chr(10) + 'y')
            git('add', '-A'); git('commit', '-qm', 'early', when='2026-01-01T12:00:00+09:00')
            def legacy(title, refs):
                p = VAULT.root / '00_Scope/W1' / (title + '.md')
                n = contract.parse(p)
                p.write_bytes(nodefile.render({**n.meta, 'derived-from': refs}, n.body))
                return p
            missing, late = rounds[0].replace('#1', '#9'), rounds[0].replace('#1', '#5')
            a = legacy('A', ['[[N]]', rounds[0], noted, '[[_sources/r.md]]', '[[' + url + ']]', missing, late])
            b = legacy('B', [rounds[1], rounds[0], notice])
            c = legacy('C', [rounds[0]])
            git('add', '-A'); git('commit', '-qm', 'seed', when='2026-02-01T12:00:00+09:00')
            assert raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': '늦은 질문', 'agent': '답'}])['round_refs'] == [late]
            state = {p: rechecks.state(p.read_bytes()) for p in (a, b, c)}
            seen = em.preview(VAULT)
            assert (seen['nodes'], seen['line'], seen['covered'], seen['hold']) == (2, 6, 1, 3), seen
            assert sorted(h['reason'].split(':')[0] for h in seen['holds']) == [
                '근거 절을 본문을 바꾸지 않고 노드 끝에 달 수 없다', '날짜를 정할 수 없다 — git 기록에 없다', '없는 라운드'], seen
            assert (VAULT.root / seen['plan']).is_file() and not em.events_path(VAULT).exists()
            done = em.apply(VAULT)
            assert (done['nodes'], done['line'], done['covered'], done['hold']) == (2, 6, 1, 3), done
            assert all(rechecks.state(p.read_bytes()) == state[p] for p in (a, b, c)), 'the related state changed'
            na, nb = contract.parse(a), contract.parse(b)
            assert na.meta['derived-from'] == ['[[N]]', missing, late] and 'derived-from' not in nb.meta, (na.meta, nb.meta)
            assert evidence.coordinates(na.body) == ['c38d8ef2#1', 'c38d8ef2#3', '_sources/r.md', url], na.body
            # rounds date from the commit that brought them; the file and the URL from the commit that put them
            # in the frontmatter, not the earlier body mention
            first = '- 2026-01-01 · `c38d8ef2#1` · 지지 — ' + ('첫 질문입니다 ' * 9)[:50].rstrip() + '…'
            lines = na.body.rstrip(chr(10)).split(chr(10))
            assert lines[-7:-4] == ['주장 A. 출처는 ' + url, '# 관측', ''] and lines[-4] == first, lines
            assert lines[-3] == '- 2026-01-01 · `c38d8ef2#3` · 지지 — 인용 요지 #272 의 결정', lines
            assert lines[-2:] == ['- 2026-02-01 · `_sources/r.md` · 지지 — r.md',
                                  '- 2026-02-01 · `' + url + '` · 지지 — page.md'], lines
            assert nb.body.rstrip(chr(10)).split(chr(10))[-3:] == [
                '- 2026-10-05 · `c38d8ef2#2` · 지지 — 이미 있다', first,
                '- 2026-01-01 · `c38d8ef2#4` · 지지 — 작업이 끝나 결과를 정리했다'], nb.body
            assert validate.evidence_errors(VAULT, nb.body, nb.meta.get('derived-from')) == []
            events = [r for r in core.ledger_read(em.events_path(VAULT)) if r.get('rule') == em.RULE]
            assert [(e['source'], e['after']) for e in events] == [
                (p.relative_to(VAULT.root).as_posix(), core.sha256_file(p)) for p in (a, b)], events
            manifest = (VAULT.root / done['manifest']).read_text(encoding='utf-8')
            assert '## 결과' in manifest and '보류 — 없는 라운드' in manifest, manifest
            assert em.plan(VAULT)['counts'] == {'line': 0, 'covered': 0, 'hold': 3}, 'a rerun moved something again'
        """)

    def test_doctor_reports_and_applies_the_remaining_migrations(self):
        # 2026-11-09 user decision: compatibility and bulk migrations live in `osk doctor` — the report only
        # reads, `--migrate` plans, then applies them in order once confirmed. v5.6 renames the `## 근거` heading to `# 관측` without
        # touching the claim, the lines, the frontmatter or the related state. A body that opens with
        # `## 근거` would read `# 관측` as its own title, so it is held and keeps the legacy heading.
        self.check_case("""
            from osk import doctor, evidence_migration as em, rechecks
            line = '- 2026-11-09 · `https://example.com/a` · 지지 — page'
            node('L', body='주장 L.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10) + line)
            node('T', body='## 근거' + chr(10) + chr(10) + line)
            node('O', body='주장 O.' + chr(10) + chr(10) + '# 관측' + chr(10) + chr(10) + line)
            node('Tab', body='주장 Tab.' + chr(10) + chr(10) + '##' + chr(9) + '근거' + chr(10) + chr(10) + line)
            # X is cited by heading range: an H1 `# 관측` would close the range of `# X`, so it is held
            node('X', body='# X' + chr(10) + chr(10) + '주장 X.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10) + line)
            assert write.create_node(VAULT, 'C', 'C', 'C relies on X.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'X#X'})['ok']
            # Y's section is only linked from another body, not cited: the link would lose its anchor (#311 review)
            node('Y', body='주장 Y.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10) + line)
            node('D', body='D reads [[Y#근거]].')
            paths = {t: VAULT.root / '00_Scope/W1' / (t + '.md') for t in ('L', 'T', 'O', 'Tab', 'X', 'Y')}
            before = {t: p.read_bytes() for t, p in paths.items()}
            vault = {i['check']: i for i in doctor._vault(VAULT)}
            heading = next(i for k, i in vault.items() if '# 관측' in k)
            assert heading['level'] == 'warn' and '2개' in heading['detail'] and '--migrate' in heading['fix'], vault
            assert all(p.read_bytes() == before[t] for t, p in paths.items()), 'the report wrote'
            # The first run only plans and asks for the user's yes; nothing moves yet.
            plan, code = doctor.migrate(VAULT)
            assert code == 2 and plan['approval_required'] and [m[0] for m in plan['plan']['migrations']] == [
                heading['check']] and len(plan['plan']['migrations'][0][1]) == 2, plan
            assert all(p.read_bytes() == before[t] for t, p in paths.items()), 'the plan wrote'
            # The apply re-plans under the lock; a node the user did not confirm stops it before any write (#313 review).
            only_l = [t for t in plan['plan']['migrations'][0][1] if t[0].endswith('/L.md')]
            try:
                em.heading_apply(VAULT, expect=only_l)
            except RuntimeError as e:
                assert 'Tab.md' in str(e), e
            else:
                raise AssertionError('migrated a node that was not confirmed')
            # A confirmed node that changed after the user's yes stops it too (#313 review, round 2).
            stale = [[p, 'sha256:0' if p.endswith('/L.md') else h] for p, h in plan['plan']['migrations'][0][1]]
            try:
                em.heading_apply(VAULT, expect=stale)
            except RuntimeError as e:
                assert 'L.md' in str(e) and 'Tab.md' not in str(e), e
            else:
                raise AssertionError('migrated a node that changed after it was confirmed')
            assert all(p.read_bytes() == before[t] for t, p in paths.items()), 'the refused apply wrote'
            assert not list(em.events_path(VAULT).parent.glob('observation-heading-*')), 'the refused apply opened a manifest'
            done, code = doctor.migrate(VAULT)
            assert code == 0 and [d['migration'] for d in done['applied']] == [heading['check']], done
            assert (done['applied'][0]['nodes'], done['applied'][0]['holds']) == (2, 3), done
            new = paths['L'].read_bytes()
            assert new == before['L'].replace('## 근거'.encode(), '# 관측'.encode()), new
            assert rechecks.state(new) == rechecks.state(before['L'])
            assert paths['Tab'].read_bytes() == before['Tab'].replace(('##' + chr(9) + '근거').encode(), '# 관측'.encode())
            assert all(paths[t].read_bytes() == before[t] for t in ('T', 'O', 'X', 'Y'))
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'the migration made recheck candidates'
            manifest = (VAULT.root / done['applied'][0]['manifest']).read_text(encoding='utf-8')
            assert '`00_Scope/W1/X.md` → 보류 — 제목으로 참조된다(`#X`)' in manifest, manifest
            assert '`00_Scope/W1/Y.md` → 보류 — 제목으로 참조된다(`#근거`)' in manifest, manifest
            assert sorted(e['source'] for e in core.ledger_read(em.events_path(VAULT))
                          if e.get('rule') == em.HEADING_RULE) == ['00_Scope/W1/L.md', '00_Scope/W1/Tab.md']
            assert all(i['level'] == 'ok' for i in doctor._vault(VAULT)), doctor._vault(VAULT)
            # nothing left: a rerun applies nothing and opens no empty manifest
            assert doctor.migrate(VAULT) == ({'ok': True, 'applied': []}, 0)
            assert len(list(em.events_path(VAULT).parent.glob('observation-heading-*.md'))) == 1
        """)

    def test_doctor_migrates_evidence_before_moving_old_place_records(self):
        # The evidence migration dates a round by `git blame` on the record's current path, so a record
        # moved first is untracked there and its entry is held. `doctor --migrate` migrates first; the
        # record move (#308) is one of its migrations since the two doctor commands became one.
        self.check_case("""
            import subprocess
            from osk import doctor, evidence, raw
            def git(*a):
                subprocess.run(['git', '-C', str(VAULT.root), *a], check=True, capture_output=True)
            git('init', '-q'); git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
            name = 'claude-c38d8ef2' + '0' * 24
            old = VAULT.root / '00_Scope/W1/_raw/.records'
            old.mkdir(parents=True)
            (old / (name + '.txt')).write_bytes(raw.round_block(1, '옛 자리의 질문', '답').encode())
            # A also keeps the legacy `## 근거` heading: the v5.4 lines land in it, then the v5.6 rename takes
            # A at the hash v5.4 wrote — the confirmed hash carried forward, not a change after the yes.
            node('A', body='주장 A.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10)
                 + '- 2026-11-09 · `https://example.com/a` · 지지 — page')
            a = VAULT.root / '00_Scope/W1/A.md'
            n = contract.parse(a)
            a.write_bytes(nodefile.render({**n.meta, 'derived-from': ['00_Scope/W1/_raw/.records/' + name + '.txt#1']}, n.body))
            git('add', '-A'); git('commit', '-qm', 'seed')
            v54, v52 = '비노드 derived-from → 관측 줄 (v5.4)', '옛 자리의 인용 기록 → 루트 `_cited/` (v5.2)'
            v56 = '관측 절 제목 `## 근거` → `# 관측` (v5.6)'
            plan, code = doctor.migrate(VAULT)
            assert code == 2 and [m[0] for m in plan['plan']['migrations']] == [v54, v52, v56], plan
            # A plan that changed after it was shown asks again: a second old-place record turned up.
            other = 'claude-d48d8ef2' + '0' * 24
            (old / (other + '.txt')).write_bytes(raw.round_block(1, '또 다른 질문', '답').encode())
            assert doctor.migrate(VAULT)[1] == 2
            rep, code = doctor.migrate(VAULT)
            applied = {a['migration']: a for a in rep['applied']}
            assert code == 0 and list(applied) == [v54, v52, v56], rep
            assert applied[v52]['count'] == 2 and applied[v54]['nodes'] == 1 and applied[v56]['nodes'] == 1, applied
            na = contract.parse(a)
            assert 'derived-from' not in na.meta and 'c38d8ef2#1' in evidence.coordinates(na.body), na.body
            assert '# 관측' in na.body and '## 근거' not in na.body, na.body
            assert raw.locate_record(VAULT, name) == VAULT.root / '_cited/.records' / (name + '.txt')
            assert doctor.migrate(VAULT) == ({'ok': True, 'applied': []}, 0)
        """)

    def test_recheck_cascade_keeps_node_identity_when_bodies_match(self):
        self.check_case("""
            from osk import rechecks
            node('A')
            for title, body, basis in (('B', 'Old B.', 'A'), ('C', 'C relies on B.', 'B'),
                                       ('X', 'Old X.', 'A'), ('Y', 'Y relies on X.', 'X')):
                assert write.create_node(VAULT, title, title, body, 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': basis})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            write.update_node(VAULT, 'B', old_text='Old B.', new_text='The same claim.',
                              rechecked=['A'])
            write.update_node(VAULT, 'X', old_text='Old X.', new_text='The same claim.')
            cascades = {i['node']: i['cascade'] for i in rechecks.candidates(VAULT, graph.Index(VAULT))[0]}
            assert cascades['C'] is True, cascades
            assert cascades['Y'] is False, cascades
        """)

    def test_recheck_pick_rotates_by_attempts_in_a_fork_scope(self):
        self.check_case("""
            node('A')
            for title in ('B1', 'B2'):
                assert write.create_node(VAULT, title, title, title + ' relies on A.', 'gpt-6-astra',
                                         space='00_Scope/W1', edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            first = growth_plan.pick_rechecks(VAULT, graph.Index(VAULT), 1, 'W1')
            register({**growth_plan.plan(VAULT, 3), 'scope_jobs': [], 'recheck_jobs': first})
            again = growth_plan.pick_rechecks(VAULT, graph.Index(VAULT), 1, 'W1')
            assert {first[0]['node'], again[0]['node']} == {'B1', 'B2'}, (first, again)
        """)

    def test_reviews_keep_current_claims_and_bind_existing_ones_by_evidence_line(self):
        # 2026-10-05: an old round must not roll back a newer claim, and a round that only
        # supports a held claim gets a `# 관측` line instead of a rewritten body.
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            worker = growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
            assert 'never replace, weaken or reorder a current claim' in worker
            assert '"# 관측" section' in worker and '42cf0fc0#1' in worker
            path = VAULT.root / 'held.jsonl'
            rows = [{'type':'user','sessionId':'held','uuid':'u1','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'held','uuid':'a1','message':{'role':'assistant','id':'m1',
                     'content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r) + '\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','held',str(path),'held-project',space='00_Scope/W1')['ok']
            own = integration.prompt(VAULT, 'claude','held')['text']
            assert '현행 주장을 옛 내용으로 바꾸거나 약화·재배열하지 않는다' in own
            assert '`# 관측` 절' in own and '`42cf0fc0#1`' in own
        """)
