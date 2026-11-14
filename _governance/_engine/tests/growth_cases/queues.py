"""성장 실행의 수트의 한 갈래: 무엇을 고르는가 — 큐의 차례와 예산.

`tests/test_growth.py`의 `GrowthTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""



class QueueCases:
    def test_first_turn_is_fair_when_budget_cannot_select_every_queue(self):
        self.check_case("""
            for limit in (3, 1, 2, 4, 5):
                rows, first = [], []
                for _ in range(8):
                    planned = {key:[{'key':key+'-0'}, {'key':key+'-1'}] for key in growth_ledger.QUEUES}
                    growth_plan.select_work(planned,limit,rows)
                    order = planned['work_order']
                    first.append(order[0]['queue'])
                    assert len(order) == limit
                    assert len({(i['queue'],i['index']) for i in order}) == limit
                    for key in growth_ledger.QUEUES:
                        assert [i['index'] for i in order if i['queue']==key] == list(range(len(planned[key])))
                        assert len(planned[key]) + planned['queued_not_selected'][key] == 2
                    rows.append({'kind':'plan',**planned})
                # Even if each worker stops after its first job, every queue
                # receives a turn within one attempt per queue.
                q = len(growth_ledger.QUEUES)
                assert all(set(first[n:n+q]) == set(growth_ledger.QUEUES) for n in range(9-q)), (limit,first)
        """)

    def test_execution_order_rotates_even_when_every_queue_is_selected(self):
        self.check_case("""
            rows, first, q = [], [], len(growth_ledger.QUEUES)
            for _ in range(2 * q):
                planned = {key:[{'key':key}] for key in growth_ledger.QUEUES}
                growth_plan.select_work(planned,q,rows)
                order = planned['work_order']
                first.append(order[0]['queue'])
                assert len(order) == q and {i['queue'] for i in order} == set(growth_ledger.QUEUES)
                rows.append({'kind':'plan',**planned})
            assert first[:q] == list(growth_ledger.QUEUES), first
            assert first[q:] == first[:q], first
            daily = {key:[{'key':key}] for key in growth_ledger.QUEUES}
            growth_plan.select_work(daily,q,[dict(row,work_context='stop:W1') for row in rows])
            assert daily['work_order'][0]['queue'] == growth_ledger.QUEUES[0], daily
        """)

    def test_eviction_settlement_needs_review_and_deferred_returns_to_queue(self):
        for deferred in (True, False):
            self.check_case("""
            from unittest.mock import patch
            from osk import evictions
            item = evictions.record_evict(VAULT, 'W1', 'session', 'Reusable fact; second claim needs verification.')
            with patch.object(evictions, 'age_days', return_value=17):
                action = "from osk import evictions, write, cluster_gate, node_checks, node_moves, nodefile; j=p['eviction_jobs'][0]; saved=write.create_node(VAULT, 'Partial', 'Partial evidence', 'Only the first claim is verified.', 'fixture', space='00_Scope/W1', settle=j['of']); assert saved['ok']; "
                if DEFERRED_CASE:
                    action += "q['osk_reviews']['eviction']=[{'of':j['of'],'outcome':'deferred','reason':'First claim saved; resume second claim against the source.'}]; assert growth_reviews.checkpoint(VAULT, q)['ok']"
                else:
                    action += "sys.exit(7)"
                result = growth.run(VAULT, [sys.executable, '-B', '-c', packet_worker(action)], limit=1)
                assert not result['ok'], result
                assert result['eviction_outcomes']['eviction:'+item['rid']]['status'] == 'pending', result
                manifest = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                job = manifest['eviction_jobs'][0]
                assert item['rid'] not in {r['rid'] for r in evictions.unsettled(VAULT)}
                again = growth_plan.plan(VAULT)['eviction_jobs']
                assert any(j['of']==item['rid'] for j in again), again
                assert again[0]['previous_settlement']['target'] == 'Partial'
                if DEFERRED_CASE:
                    assert again[0]['previous_deferral']['reason'].startswith('First claim saved')
                # M4: the review is checked against the settlement already recorded. A different
                # disposition or target is set aside and the eviction stays open.
                for outcome, target in (('merged', 'Partial'), ('node', 'Elsewhere')):
                    mismatch = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                                'eviction':[{'of':item['rid'],'outcome':outcome,'target':target,
                                             'reason':'Disagrees with the recorded settlement.'}]}}
                    refused = growth_reviews.checkpoint(VAULT, mismatch)
                    assert not refused['ok'] and any('different disposition' in e for e in refused['errors']), refused
                    assert growth_plan.eviction_status(VAULT, job)['status'] == 'pending'
                packet = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                          'eviction':[{'of':item['rid'],'outcome':'node','target':'Partial',
                                       'reason':'Both claims now checked; second is a one-off value.'}]}}
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert growth_plan.eviction_status(VAULT, job)['status'] == 'complete'
                count = len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='eviction_review'])
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='eviction_review']) == count
                assert not growth_plan.plan(VAULT)['eviction_jobs']
                # A later explicit deferral must supersede even a completed review.
                packet['osk_reviews']['eviction'] = [{'of':item['rid'],'outcome':'deferred','reason':'Reopen a newly disputed condition.'}]
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert growth_plan.eviction_status(VAULT, job)['status'] == 'pending'
                assert growth_plan.plan(VAULT)['eviction_jobs'][0]['of'] == item['rid']
                packet['osk_reviews']['eviction'] = [{'of':item['rid'],'outcome':'node','target':'Partial','reason':'Dispute resolved against the source.'}]
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
            """.replace('DEFERRED_CASE', repr(deferred)))

    def test_unfinished_organization_is_remaining_work_not_a_failed_run(self):
        self.check_case("""
            import subprocess
            node('A')
            node('B')
            # Organization reads a scope in several passes (Mechanism §9-4 4): a pass that leaves
            # units is progress. After v5.0.0 the scheduler logged every such daily run as failed
            # and the session start said the daily run had not run at all.
            command_file = VAULT.root/'command.json'
            command_file.write_text(json.dumps([sys.executable,'-c',packet_worker(org='deferred')]),
                                    encoding='utf-8')
            proc = subprocess.run([sys.executable,'-B','-m','osk.cli','growth','run',
                                   '--command-file',str(command_file),'--limit','3'],
                                  capture_output=True,text=True,encoding='utf-8',timeout=120)
            result = json.loads(proc.stdout)
            assert result['state'] == 'incomplete' and not result['final_reviews']['errors'], result
            assert {s['status'] for s in result['organization_outcomes'].values()} == {'pending'}, result
            assert proc.returncode == 0 and not growth_ledger.run_failed(result), (proc.returncode, result)
            # #301: an organization left over is no reason to hand rechecks to the fork.
            status = growth_ledger.daily_status(VAULT)
            assert status['ran'] and status['active'], status
            assert status['last']['state'] == 'incomplete', status
            # A run that could not run, or whose worker or packet failed, still fails.
            assert growth_ledger.run_failed({**result, 'returncode': 1})
            assert growth_ledger.run_failed({**result, 'final_reviews': {'state': 'rejected', 'errors': ['no packet']}})
            assert growth_ledger.run_failed({'ok': False, 'state': 'unavailable'})
            assert not growth_ledger.run_failed({'ok': True, 'state': 'skipped', 'selected': 0})
            # Another run holding the lock is a wait, not a failure (#175: 10-05 and 10-06 ended busy).
            assert not growth_ledger.run_failed({'ok': False, 'state': 'busy'})
        """)

    def test_scope_tries_count_only_what_the_daily_run_judged(self):
        self.check_case("""
            # #306 D4: picks counted waits, failures and fork attempts and never shrank before progress,
            # so conversations tried once by a failing run or a fork stood at the back of the queue.
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            ago = lambda days: (now - timedelta(days=days)).isoformat(timespec='seconds')
            job = {'harness': 'claude', 'conversation_id': 'c', 'reviewed_rounds': 0}
            state = {'harness': 'claude', 'conversation_id': 'c', 'reviewed_count': 0}
            rows = []
            def attempt(run=None, context='daily'):
                rid = 'p' + str(len(rows))
                rows.append({'kind': 'plan', 'rid': rid, 'work_context': context, 'scope_jobs': [job]})
                if run is not None:
                    rows.append({'kind': 'run', 'manifest': rid, 'returncode': 0, 'at': ago(20), **run})
            attempt({'state': 'incomplete'})                       # judged, however old
            attempt({'state': 'limited', 'returncode': 1})         # waited on a limit
            attempt({'state': 'busy'})                             # waited on the lock
            attempt({'state': 'incomplete', 'returncode': 1})      # the worker failed, three weeks ago
            attempt()                                              # no result
            attempt({'state': 'incomplete'}, context='stop:W1')    # a fork's own attempt
            assert growth_plan.scope_tried(rows)(state) == 1
            # A recent failure still counts: a conversation that keeps failing gives others their turn (#136 M4).
            attempt({'state': 'incomplete', 'returncode': 1, 'at': ago(1)})
            assert growth_plan.scope_tried(rows)(state) == 2
        """)

    def test_daily_status_keeps_a_plan_without_result_as_ran_not_completed(self):
        self.check_case("""
            assert growth_ledger.daily_status(VAULT) == {'ran': False, 'active': False, 'last': None}
            planned = register()
            status = growth_ledger.daily_status(VAULT)
            assert status['ran'] and not status['active'] and status['last']['state'] is None, status
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':planned['rid'],'ok':True,
                                               'state':'complete'})
            assert growth_ledger.daily_status(VAULT)['active'] and growth_ledger.daily_active(VAULT)
        """)

    def test_scope_job_reading_instruction_follows_the_run_context(self):
        self.check_case("""
            # A daily worker took the Stop-fork clause for itself and judged turns it never read
            # (v5.0.0 operation, both daily scope reviews). Each run carries only its own case.
            job = {'harness':'claude','conversation_id':'c','pending_refs':['native:claude:c:t'],
                   'through':'sha256:x','key':'scope-review-x'}
            base = {'candidates':[],'scope_jobs':[job],'organization_jobs':[],'eviction_jobs':[],
                    'recheck_jobs':[]}
            daily = growth.prompt(VAULT, {**base,'work_context':'daily'})
            stop = growth.prompt(VAULT, {**base,'work_context':'stop:W1'})
            assert 'not in your context' in daily and 'read_cited(ref=<that ref>)' in daily, daily
            assert 'Stop fork' not in daily and 'still in your context' not in daily, daily
            assert 'still in your context' in stop and 'read_cited(ref=<that ref>)' not in stop, stop
            # Compaction drops a backlog from the fork's context too: it reads the range once (#226).
            assert 'read_cited(conversation=' in stop and 'through=<its through>' in stop, stop
            assert 'through=<its through>' not in daily, daily
            assert 'not in your context' in growth.prompt(VAULT, base), 'a preview is the daily inventory'
        """)

    def test_unrelated_capture_error_does_not_fail_completed_selected_work(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            from unittest.mock import patch
            node('A')
            catchup = {'ok':False,'jobs':[],'captures':[], 'remaining':1,
                       'errors':[{'conversation_id':'other','error':'native identity mismatch'}]}
            with patch.object(integration,'catchup',return_value=catchup):
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
                assert result['ok'] and result['selected'], result
                assert not result['capture']['ok'] and result['capture']['errors']==catchup['errors']
                assert growth.run(VAULT, ['unused-command'])['state']=='capture_pending'
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u',
                'message':{'role':'user','content':'question'}})+'\\n'+json.dumps(
                {'type':'assistant','sessionId':'own','uuid':'a','message':{'role':'assistant',
                 'id':'m','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}})+'\\n',encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','own',str(native),'own',space='00_Scope/W1')['ok']
            job = integration.prompt(VAULT, 'claude','own')
            job.update(ok=False,capture_error='selected conversation capture failed')
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],scope_job=job)
            assert not result['ok'], 'the selected conversation capture error remains a blocker'
        """)

    def test_new_source_and_content_changes_reopen_comparisons(self):
        self.check_case("""
            first = node('A')
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
            node('B')
            candidates = growth_plan.plan(VAULT)['candidates']
            assert any({s['name'] for s in c['sources']} == {'A','B'} for c in candidates)
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
            result = write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            assert result['ok'], result
            assert growth_plan.plan(VAULT)['candidates']
        """)

    def test_cross_scope_old_new_and_completed_batch_progress(self):
        self.check_case("""
            for name in ('A','B','C','D','E'):
                node(name)
            first = growth_plan.plan(VAULT, 1)
            assert len(first['candidates']) == 1
            manifest = register(first)
            growth_reviews.review(VAULT, first['candidates'][0]['key'], 'no_value', reason='No value in first batch', manifest=manifest['rid'])
            second = growth_plan.plan(VAULT, 1)
            assert second['candidates'][0]['key'] != first['candidates'][0]['key']
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
            node('Z', 'W2')
            candidates = growth_plan.plan(VAULT, 20)['candidates']
            assert any({'A','Z'} <= {s['name'] for s in c['sources']} for c in candidates)
            assert all(len(c['sources']) <= 8 for c in candidates)
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
        """)

    def test_deferred_candidates_rotate(self):
        self.check_case("""
            for name in ('A','B','C','D','E','F','G','H','I'):
                node(name)
            tried = []
            for _ in range(4):
                selected = register(growth_plan.plan(VAULT, 1))
                candidate = selected['candidates'][0]
                assert candidate['key'] not in tried, tried
                tried.append(candidate['key'])
                growth_reviews.review(VAULT, candidate['key'], 'deferred', reason='Need independent corroboration', manifest=selected['rid'])
            assert growth_plan.plan(VAULT)['candidates']
        """)

    def test_new_versions_get_priority_without_starving_old_comparisons(self):
        self.check_case("""
            for name in ('A','B','C','D','E','F','G','H','I'):
                node(name)
            inventory = growth_plan.plan(VAULT, 20)
            register(dict(inventory, candidates=[c for c in inventory['candidates']
                                                if c['grouping']=='comparison']))
            # Sources were attempted in pairs, but individual batches remain untried.
            node('Z', 'W2')
            first = growth_plan.plan(VAULT, 2)
            assert any(s['name']=='Z' for s in first['candidates'][0]['sources']), first
            assert all(s['name']!='Z' for s in first['candidates'][1]['sources']), first
            assert growth_plan.plan(VAULT, 2)==first, 'preview consumed the new version'
            register(first)
            assert write.update_node(VAULT, 'I', old_text='I reusable observation', new_text='I changed')['ok']
            second = growth_plan.plan(VAULT, 2)
            assert any(s['name']=='I' for s in second['candidates'][0]['sources']), second
            assert all(s['name']!='I' for s in second['candidates'][1]['sources']), second
            register(second)
            picked = []
            for i in range(4):
                node('Z'+str(i), 'W2')  # continuous new content, limit=1 still rotates
                selected = register(growth_plan.plan(VAULT, 1))['candidates'][0]
                picked.append({s['name'] for s in selected['sources']})
            assert any(names <= set('ABCDEFGHI') for names in picked), picked
        """)

    def test_short_finished_transcript_waits_for_its_own_fork(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            path = VAULT.root / 'native.jsonl'
            user = {'type':'user','sessionId':'short','uuid':'u1','message':{'role':'user','content':'Report this temporary completed job.'}}
            final = {'type':'assistant','sessionId':'short','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'Temporary job finished.'}],'stop_reason':'end_turn'}}
            path.write_text(json.dumps(user)+'\\n',encoding='utf-8')
            state = conversation_capture.capture(VAULT, 'claude','short',str(path),'short-project',space='00_Scope/W1')
            assert state['capture_pending'], state
            with path.open('a',encoding='utf-8') as f:
                f.write(json.dumps(final)+'\\n')
            # The daily run tracks the finished turn but has no copy of it to review.
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
            assert conversation_capture.status(VAULT, 'claude','short')['captured_rounds'] == 1
            # The conversation's own Stop fork holds the turn and reviews it.
            job = integration.prompt(VAULT, 'claude','short',include_organization=False)
            for change in (
                    "q['osk_reviews']['scope'][0]['through']='stale-snapshot'",
                    "q['osk_reviews']['scope'][0].update(outcome='preserved',targets=[{'key':'missing-proof'}])"):
                rejected = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],scope_job=job)
                assert not rejected['ok'] and rejected['final_reviews']['errors'], rejected
            worker = packet_worker()
            result = growth.run(VAULT, [sys.executable,'-c',worker],scope_job=job)
            assert result['ok'], result
            assert result['scope_selected'] == 1 and result['domain_selected'] == 0, result
            assert {r['status'] for r in result['scope_outcomes'].values()} == {'complete'}
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
        """)

    def test_daily_run_reads_an_ended_conversation_and_restarts_its_cadence(self):
        self.check_case("""
            import os, time
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            path = VAULT.root / 'ended.jsonl'
            rows = []
            for i in range(1, 4):
                rows += [{'type':'user','sessionId':'ended','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'ended','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','ended',str(path),'ended-project',space='00_Scope/W1')['ok']
            for _ in range(5):
                conversation_capture.tick(VAULT, 'claude','ended')
            # Still running: its turns stay with the conversation's own review cadence.
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
            old = time.time() - conversation_capture.ENDED_AFTER - 60
            os.utime(path, (old, old))
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
            assert result['ok'] and result['scope_selected'] == 1, result
            plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            job = growth._reading_plan(plan)['scope_jobs'][0]
            assert job['pending_refs'] == ['native:claude:ended:u' + str(i) for i in (1, 2, 3)], job
            assert 'read_command' not in job, job
            assert 'read_cited(ref=' in growth.prompt(VAULT, plan), 'a sandboxed worker reads through MCP'
            state = conversation_capture.status(VAULT, 'claude','ended')
            assert (state['reviewed_rounds'], state['pending']) == (3, False), state
            assert state['prompt_count'] == state['reviewed_prompt_count'] == 5, state
        """)

    def test_prompt_units_carry_the_view_hash_read_node_returns(self):
        self.check_case("""
            # The worker sees only this prompt. On 2026-10-04 it compared view_hash with the sha256
            # inside each unit (a range content key) and deferred every range it had read.
            import mcp_server as M
            node('A')
            node('B')
            text = growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
            assert organization.guidance() in text and 'expect_view_hash' in organization.guidance()
            units = [u for j in json.loads(text.rsplit(chr(10), 1)[1])['organization_jobs'] for u in j['review_units']]
            assert units
            for u in units:
                got = M.read_node(name=u['id'], view=u['view'])['view_hash']
                assert u['expect_view_hash'] == got, (u, got)
                assert u['unit'].split(':')[2] not in got, u
        """)

    def test_total_budget_and_queue_rotation_do_not_ack_unselected_scope(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            path = VAULT.root / 'native.jsonl'
            rows = []
            for i in range(12):
                rows += [{'type':'user','sessionId':'bounded','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'bounded','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(row)+'\\n' for row in rows),encoding='utf-8')
            conversation_capture.capture(VAULT, 'claude','bounded',str(path),'bounded-project',space='00_Scope/W1')
            legacy('bounded', path)
            original = conversation_capture.status(VAULT, 'claude','bounded')
            seen = []
            worker = [sys.executable,'-c','import sys; sys.stdin.read()']  # no ACKs
            for i in range(3):
                result = growth.run(VAULT, worker,limit=1)
                assert result['selected'] == 1 and not result['ok'], result
                plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                queue = next(k for k in growth_ledger.QUEUES if plan[k])
                seen.append(queue)
                if plan['scope_jobs']:
                    job = plan['scope_jobs'][0]
                    assert len(job['pending_refs']) == 9 and job['remaining_rounds'] == 3, job  # 9 rounds a job (#175)
                    assert job['through'] != original['through']
                    visible = growth._reading_plan(plan)['scope_jobs'][0]
                    assert 'prompt' not in visible and 'last_review' not in visible
                    assert visible['through'] == job['through'] and visible['pending_refs'] == job['pending_refs']
                    job['last_review'] = {'through':job['through'], 'outcome':'deferred', 'reason':'Search CACHE_LIMIT retest first.'}
                    visible = growth._reading_plan(plan)['scope_jobs'][0]
                    assert visible['previous_deferral']['reason'] == job['last_review']['reason']
            assert seen == ['candidates', 'scope_jobs', 'organization_jobs'], seen
            assert conversation_capture.status(VAULT, 'claude','bounded')['pending_refs'] == original['pending_refs']
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
            assert result['ok'] and result['selected'] == 3, result
            state = conversation_capture.status(VAULT, 'claude','bounded')
            assert len(state['pending_refs']) == 3, state  # selected nine only, not the whole conversation
        """)

    def test_scope_queue_goes_by_turn_time_and_passes_a_stalled_conversation(self):
        self.check_case("""
            import os, time
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            # Two ended conversations compete for one Scope slot per run (limit=3 shares the
            # budget with the candidate and organization queues). 'early' holds the older turns
            # although its state was touched last, so time order picks it first. A run that ends
            # without an ACK leaves it in place; the next run gives 'late' its turn instead of
            # choosing 'early' again (#136 M4: six such runs all chose the first conversation).
            ended = time.time() - conversation_capture.ENDED_AFTER - 60
            for n, (sid, day) in enumerate((('late', '02'), ('early', '01'))):
                path = VAULT.root / (sid + '.jsonl')
                rows = []
                for i in range(1, 3):
                    rows += [{'type':'user','sessionId':sid,'uuid':sid+'-u'+str(i),'timestamp':'2026-10-'+day+'T0'+str(i)+':00:00Z','message':{'role':'user','content':'question '+str(i)}},
                             {'type':'assistant','sessionId':sid,'uuid':sid+'-a'+str(i),'message':{'role':'assistant','id':sid+'-m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
                assert conversation_capture.capture(VAULT, 'claude',sid,str(path),sid+'-project',space='00_Scope/W1')['ok']
                os.utime(path, (ended, ended))
                os.utime(conversation_state.state_path(VAULT, 'claude', sid), (ended + n, ended + n))
            worker = [sys.executable,'-c','import sys; sys.stdin.read()']   # no ACKs
            picks = []
            for _ in range(3):
                result = growth.run(VAULT, worker, limit=3)
                assert not result['ok'], result
                plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                picks.append(plan['scope_jobs'][0]['conversation_id'])
            assert picks == ['early', 'late', 'early'], picks
        """)

    def test_conversations_that_yield_no_job_do_not_hold_the_catch_up_slots(self):
        self.check_case("""
            import os, time
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            os.environ['CODEX_HOME'] = str(VAULT.root / 'codex-home')    # never the real sessions
            # Codex conversations whose original transcripts are gone still date their next turn
            # from the stored UUIDv7 turn ids, so they sort first, yet catch-up cannot build jobs
            # from them. Catch-up walks the whole sorted queue to the next conversation that yields
            # a job: neither the run limit nor a lookup cap lets them hold the slots (#152 review:
            # with nothing else queued, run after run reviewed nothing and B waited).
            ended = time.time() - conversation_capture.ENDED_AFTER - 60
            path = VAULT.root / 'b.jsonl'
            rows = [{'type':'user','sessionId':'b','uuid':'b-u1','timestamp':'2026-10-02T01:00:00Z','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'b','uuid':'b-a1','message':{'role':'assistant','id':'b-m1','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','b',str(path),'b-project',space='00_Scope/W1')['ok']
            os.utime(path, (ended, ended))
            for i in range(101):
                sid, turn = 'lost' + str(i), '01900000-0000-7000-8000-%012d' % i
                with conversation_state.state_lock(VAULT, 'codex', sid) as p:
                    s = conversation_state.load_state(VAULT, p, 'codex', sid)
                    s.update(session='lost-project', space='00_Scope/W1', prompt_count=1,
                             transcript_path=str(VAULT.root / 'gone.jsonl'),
                             rounds=[{'id':sid+'-u1:'+turn, 'ref':'native:codex:'+sid+':'+turn,
                                      'hash':'sha256:' + '0'*64, 'completion':'completed'}])
                    conversation_state.save_state(p, s)
            first = [x['conversation_id'] for x in integration._known_pending(VAULT, 100)[0]]
            assert len(first) == 100 and 'b' not in first, first[-3:]
            result = growth.run(VAULT, [sys.executable,'-c','import sys; sys.stdin.read()'], limit=1)
            assert result.get('state') != 'capture_pending', result
            plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in plan['scope_jobs']] == ['b'], plan['scope_jobs']
            assert conversation_capture.status(VAULT, 'codex','lost0')['capture_error'], 'the failure stays visible'
        """)

    def test_conversation_review_carries_its_scope_hub_tree(self):
        self.check_case("""
            from osk import integration, organization, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            # Mechanism §9-4 3: the conversation review decides node splitting and placement and
            # carries its scope's hub tree; hub differentiation is left to the organization review.
            path = VAULT.root / 'native.jsonl'
            rows = [{'type':'user','sessionId':'tree','uuid':'u1','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'tree','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','tree',str(path),'tree-project',space='00_Scope/W1')['ok']
            job = integration.prompt(VAULT, 'claude','tree')
            tree = organization.hub_tree(VAULT, organization.scope_path(VAULT, 'W1'))
            assert job['hub_tree'] == tree and tree.startswith('- 00_Scope/W1 · 직속 노드 '), job['hub_tree']
            assert '이 scope의 허브 트리:' in job['text'] and tree in job['text']
            assert '하위 군집을 열지 않는다' in job['text']
            # Bylaws §3 8 (#273 question 3): other scopes' and Domain claims reach the review only by search.
            assert '노드를 새로 쓰기 전에 반드시 그 주제로 search' in job['text']
            reading = growth._reading_plan({'scope_jobs': [job], 'organization_jobs': []})
            assert reading['scope_jobs'][0]['hub_tree'] == tree
            # The packet drops the hook text, so the range reaches a Stop fork as a field (#226).
            assert job['range'] == '1번째 턴 1개(`u1`, 전체 1턴)', job.get('range')
            assert reading['scope_jobs'][0]['range'] == job['range']
            assert 'hub_tree' in growth.prompt(VAULT)
        """)

    def test_every_queue_and_its_later_candidate_get_turns_while_nothing_completes(self):
        self.check_case("""
            import os, time
            from unittest.mock import patch
            from osk import evictions, integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            # #136 M4 fixture: two candidates in each of the five queues and a worker that never
            # acknowledges or submits a packet. Within five runs of limit=3 every queue is chosen,
            # and so is the second candidate of every queue: a stalled first one holds no budget.
            node('A')
            node('B', 'W2')
            for title in ('R1', 'R2'):
                assert write.create_node(VAULT, title, title, title + ' relies on A.', 'gpt-6-astra',
                                         space='00_Scope/W1', edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            for i in range(2):
                evictions.record_evict(VAULT, 'W1', 'session', 'Temporary observation ' + str(i))
            ended = time.time() - conversation_capture.ENDED_AFTER - 60
            for sid in ('c1', 'c2'):
                path = VAULT.root / (sid + '.jsonl')
                rows = [{'type':'user','sessionId':sid,'uuid':sid+'-u','message':{'role':'user','content':'question'}},
                        {'type':'assistant','sessionId':sid,'uuid':sid+'-a','message':{'role':'assistant','id':sid+'-m','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
                assert conversation_capture.capture(VAULT, 'claude', sid, str(path), sid + '-project', space='00_Scope/W1')['ok']
                os.utime(path, (ended, ended))
            ident = {'candidates': 'key', 'scope_jobs': 'conversation_id', 'organization_jobs': 'scope',
                     'eviction_jobs': 'of', 'recheck_jobs': 'key'}
            assert set(ident) == set(growth_ledger.QUEUES)
            worker = [sys.executable, '-c', 'import sys; sys.stdin.read()']    # never acknowledges
            seen = {k: [] for k in ident}
            with patch.object(evictions, 'age_days', return_value=17):
                planned = growth_plan.plan(VAULT, 3)       # Scope jobs join at run time, from the conversations
                assert all(len(planned[k]) >= 2 for k in ident if k != 'scope_jobs'), planned
                assert len(integration._known_pending(VAULT, 3)[0]) == 2
                for _ in range(5):
                    result = growth.run(VAULT, worker, limit=3)
                    assert not result['ok'] and result['selected'] == 3, result
                    plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                    for k, field in ident.items():
                        seen[k] += [j[field] for j in plan[k]]
            assert all(len(set(v)) >= 2 for v in seen.values()), seen
        """)

    def test_only_dispatched_organization_jobs_advance_attempts(self):
        self.check_case("""
            from unittest.mock import patch
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            for name, scope in (('A','W1'),('B','W2'),('C','W3')):
                node(name, scope)
            path = VAULT.root / 'native.jsonl'
            rows = [
                {'type':'user','sessionId':'waiting','uuid':'u1','message':{'role':'user','content':'Keep this scoped fact.'}},
                {'type':'assistant','sessionId':'waiting','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'A bounded fact.'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows),encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','waiting',str(path),'waiting',space='00_Scope/W1')['ok']
            legacy('waiting', path)
            worker = "import sys; from osk import core,growth,organization, growth_ledger, growth_plan, growth_reviews, worker_process; from osk.vault import Vault; VAULT = Vault.open(); sys.stdin.read(); p=[r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]; [organization.review(VAULT, j['key'],j['scope'],'deferred','Needs a later targeted review.') for j in p['organization_jobs']]"
            visited = []
            for i in range(8):
                before = organization.load_state(VAULT)['plans']
                # Inventories and previews must never create/refresh attempts.
                growth_plan.plan(VAULT, 3)
                assert organization.load_state(VAULT)['plans'] == before
                with patch.object(core, 'now_kst', return_value=f'2026-09-20T12:00:{i:02d}+09:00'):
                    result = growth.run(VAULT, [sys.executable,'-c',worker],limit=3)
                assert result['selected'] == 3 and not result['ok'], result
                manifest = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                jobs = manifest['organization_jobs']
                assert len(jobs) == 1 and len(manifest['scope_jobs']) == len(manifest['candidates']) == 1, manifest
                selected = jobs[0]
                visited.append(selected['scope'])
                after = organization.load_state(VAULT)['plans']
                assert after[selected['key']]['last_attempt'] == f'2026-09-20T12:00:{i:02d}+09:00'
                assert {k:v for k,v in after.items() if v['scope'] != selected['scope']} == {
                    k:v for k,v in before.items() if v['scope'] != selected['scope']}, (before, after)
                assert organization.load_state(VAULT)['reviews'][selected['scope']]['outcome'] == 'deferred'
            assert visited == ['W1','W2','W3','W1','W2','W3','W1','W2'], visited
            assert conversation_capture.status(VAULT, 'claude','waiting')['reviewed_rounds'] == 0
        """)

    def test_stop_fork_skips_jobs_that_stalled_twice_this_week(self):
        # #259: a Stop fork attached the same organization job 8 times without progress.
        self.check_case("""
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            rows = []
            def run(days, job, made=False, **run_fields):
                queue = 'organization_jobs' if job['key'].startswith('org') else 'recheck_jobs'
                plan = {'kind':'plan','rid':'p%d' % len(rows),queue:[job]}
                rows.extend([plan, {'kind':'run','manifest':plan['rid'],'state':'incomplete','returncode':0,
                                    'at':(now - timedelta(days=days)).isoformat(timespec='seconds'),
                                    'progress':{'made':[job['key']] if made else [],'none':[] if made else [job['key']]},
                                    **run_fields}])
            a, b = {'key':'org-a','snapshot':'s1'}, {'key':'org-b','snapshot':'t1'}
            rc1 = {'key':'rc-1','node_state':'n1','target_state':'t1'}
            run(9, a); run(8, a)                                  # last week: not counted
            run(3, a); run(2, a)                                  # twice this week
            run(3, b); run(2, b, made=True); run(1, b)            # progress resets the count
            run(1, rc1); run(0, rc1)
            # The worker never reviewed these: refused by its limit, or the run failed (review of #268).
            c, d = {'key':'org-c'}, {'key':'org-d'}
            run(1, c, state='limited', returncode=1); run(0, c, state='limited', returncode=1)
            run(1, d, returncode=1); run(0, d, final_reviews={'state':'rejected','errors':['bad packet']})
            stalled = growth_plan.stalled(rows)
            assert stalled == {('org-a',None,None), ('rc-1','n1','t1')}, stalled
            assert growth_plan.skip_stalled([c, d], stalled, 2, {}) == [c, d]
            planned = {}
            # A stalled first candidate gives way to the next one (review of #268).
            assert growth_plan.skip_stalled([a, b], stalled, 1, planned) == [b], planned
            # Any write in the scope changes an organization snapshot, so the job stays stalled (#268).
            assert growth_plan.skip_stalled([dict(a, snapshot='s2'), b], stalled, 1, {}) == [b]
            assert growth_plan.skip_stalled([rc1, {'key':'rc-2'}, {'key':'rc-3'}], stalled, 1, planned) == [{'key':'rc-2'}]
            # A recheck keeps its key when the cited node or its basis changes: either state makes a new place.
            assert growth_plan.skip_stalled([dict(rc1, target_state='t2')], stalled, 1, planned) == [dict(rc1, target_state='t2')]
            assert growth_plan.skip_stalled([dict(rc1, node_state='n2')], stalled, 1, planned) == [dict(rc1, node_state='n2')]
            assert planned['stalled'] == ['org-a', 'rc-1'], planned
        """)

    def test_aged_evictions_get_bounded_slots_and_explicit_decisions(self):
        self.check_case("""
            from unittest.mock import patch
            from osk import evictions
            node('A')
            items = [evictions.record_evict(VAULT, 'W1', 'session', 'Temporary observation '+str(i)) for i in range(3)]
            assert not growth_plan.plan(VAULT)['eviction_jobs']  # Young entries retain the session-hook path.
            with patch.object(evictions, 'age_days', return_value=17):
                preview = growth_plan.plan(VAULT, 1)
                assert preview['eviction_jobs'][0]['of'] == items[0]['rid']
                worker = [sys.executable, '-B', '-c', 'import sys; sys.stdin.read()']
                selected = []
                for _ in range(3):
                    result = growth.run(VAULT, worker, limit=1)
                    assert result['selected'] == 1 and not result['ok'], result
                    manifest = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                    selected.extend(manifest['eviction_jobs'])
                assert len(selected) == 1 and selected[0]['of'] == items[0]['rid'], selected
                assert growth_plan.plan(VAULT, 1)['eviction_jobs'][0]['of'] == items[1]['rid']  # Unattempted work progresses.
                assert len(evictions.unsettled(VAULT)) == 3
                packet = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                          'eviction':[{'of':items[0]['rid'],'outcome':'discarded',
                                       'reason':'Only the completed fixture job counter; no durable claim.'}]}}
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert len(evictions.unsettled(VAULT)) == 2
                assert growth_plan.eviction_status(VAULT, selected[0])['status'] == 'complete'
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert len([r for r in evictions.records(VAULT) if r['kind']=='settle']) == 1
                packet['osk_reviews']['eviction'][0]['of'] = items[1]['rid']
                refused = growth_reviews.checkpoint(VAULT, packet)
                assert not refused['ok'] and any('unselected eviction review' in e for e in refused['errors']), refused
                assert len([r for r in evictions.records(VAULT) if r['kind']=='settle']) == 1
        """)
