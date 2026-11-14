"""성장 실행의 수트의 한 갈래: 작업자의 결과 묶음 — 읽기, 받기, 거절.

`tests/test_growth.py`의 `GrowthTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""



class PacketCases:
    def test_cli_checkpoint_survives_later_worker_failure_without_closing_other_jobs(self):
        self.check_case("""
            node('A', 'W1')
            node('B', 'W2')
            change = "from pathlib import Path; import subprocess; q['osk_reviews']['domain']=q['osk_reviews']['domain'][:1]; q['osk_reviews']['scope']=[]; checkpoint=VAULT.root/'checkpoint.json'; checkpoint.write_text(json.dumps(q),encoding='utf-8'); done=subprocess.run([sys.executable,'-B','-m','osk.cli','growth','checkpoint','--file',str(checkpoint)],capture_output=True,text=True); assert done.returncode==0 and json.loads(done.stdout)['ok'],done; sys.exit(7)"
            result = growth.run(VAULT, [sys.executable, '-B', '-c', packet_worker(change)], limit=3)
            assert not result['ok'] and result['returncode'] == 7, result
            # M2: the runner applied nothing from this failed worker; the recorded review came
            # through the manual checkpoint, and the run record keeps the two apart.
            assert result['final_reviews']['state'] == 'not_applied', result['final_reviews']
            assert list(result['domain_outcomes'].values()).count('no_value') == 1, result
            assert 'pending' in result['domain_outcomes'].values(), result
            rows = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind'] == 'review']
            assert len(rows) == 1
            packet = json.loads((VAULT.root/'checkpoint.json').read_text(encoding='utf-8'))
            assert growth_reviews.checkpoint(VAULT, packet)['ok']  # Retry is idempotent.
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 1
            packet['osk_reviews']['domain'][0]['key'] = 'unselected'
            refused = growth_reviews.checkpoint(VAULT, packet)   # only that entry is set aside, with its reason
            assert not refused['ok'] and any('unselected domain review' in e for e in refused['errors']), refused
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 1
            # The manual path applies decisions only to a plan the runner recorded.
            packet['osk_reviews']['manifest'] = 'not-a-recorded-plan'
            try:
                growth_reviews.checkpoint(VAULT, packet)
                raise AssertionError('a checkpoint applied decisions without a recorded plan')
            except ValueError as e:
                assert 'recorded growth manifest' in str(e), e
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 1
        """)

    def test_scope_checkpoint_survives_worker_failure_and_resumes_only_pending_work(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            for sid in ('first', 'second'):
                path = VAULT.root / (sid + '.jsonl')
                rows = [
                    {'type':'user','sessionId':sid,'uuid':sid+'-u','message':{'role':'user','content':'fixture question'}},
                    {'type':'assistant','sessionId':sid,'uuid':sid+'-a','message':{
                        'role':'assistant','id':sid+'-m','content':[{'type':'text','text':'fixture answer'}],
                        'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(row)+'\\n' for row in rows), encoding='utf-8')
                assert conversation_capture.capture(VAULT, 'claude',sid,str(path),'checkpoint-fixture',space='00_Scope/W1')['ok']
                legacy(sid, path)
            change = "q['osk_reviews']['domain']=[]; q['osk_reviews']['scope']=q['osk_reviews']['scope'][:1]; (VAULT.root/'saved-checkpoint.json').write_text(json.dumps(q),encoding='utf-8'); assert growth_reviews.checkpoint(VAULT, q)['ok']; sys.exit(7)"
            result = growth.run(VAULT, [sys.executable,'-B','-c',packet_worker(change)],limit=3)
            assert not result['ok'] and result['returncode'] == 7, result
            first_plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in first_plan['scope_jobs']] == ['first']
            assert conversation_capture.status(VAULT, 'claude','first')['reviewed_rounds'] == 1
            packet = json.loads((VAULT.root/'saved-checkpoint.json').read_text(encoding='utf-8'))
            assert growth_reviews.checkpoint(VAULT, packet)['ok']
            before = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'claude','first'),'claude','first')
            assert len(before['reviews']) == 1
            retry = growth.run(VAULT, [sys.executable,'-B','-c','import sys; sys.stdin.read()'],limit=3)
            assert not retry['ok']
            next_plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in next_plan['scope_jobs']] == ['second'], next_plan
            visible = growth._reading_plan(next_plan)['scope_jobs'][0]
            assert visible['raw_review'] == {'state':'verified','rounds':1,'error':None}
            assert visible['capture_recovery']['state'] == 'ready'
            after = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'claude','first'),'claude','first')
            assert after == before
            assert conversation_capture.status(VAULT, 'claude','second')['reviewed_rounds'] == 0
        """)

    def test_final_packet_reads_a_trailing_block_not_prose_or_fences(self):
        self.check_case("""
            # #306 D1: four of seven rejected packets were valid JSON after one line of explanation.
            packet = {'osk_reviews': {'manifest': 'm', 'domain': [], 'scope': []}}
            body = json.dumps(packet, indent=2)
            out = VAULT.root / 'stdout.txt'
            def final(text):
                out.write_text(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                           'result': text}) + chr(10), encoding='utf-8')
                return growth_reviews._final_packet(out)
            nl = chr(10)
            assert final(body) == packet
            assert final('Reviewed both jobs.' + nl + body) == packet
            assert final('Reviewed both jobs.' + nl + nl + body + nl) == packet
            for text in ('Here: ' + body,                                   # not at a line start
                         body + nl + 'Done.',                               # prose after it
                         'See' + nl + body + nl + 'and more' + nl,          # in the middle of prose
                         'Result:' + nl + '```json' + nl + body + nl + '```',   # in a code fence
                         'Example:' + nl + '```json' + nl + body):                # in a fence left open
                try:
                    final(text)
                except ValueError:
                    continue
                raise AssertionError(text)
        """)

    def test_stale_manifest_and_unrecorded_key_are_rejected(self):
        self.check_case("""
            node('A')
            candidate = growth_plan.plan(VAULT)['candidates'][0]
            try:
                growth_reviews.review(VAULT, candidate['key'], 'no_value', reason='Not registered', manifest='unknown')
                raise AssertionError('unrecorded review accepted')
            except ValueError:
                pass
            manifest = register()
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='Changed after selection')
            # A decision on the selected sources is the reviewer's report and is kept, but it does
            # not complete a comparison whose sources changed; those form a new comparison.
            for outcome in ('no_value','deferred'):
                growth_reviews.review(VAULT, candidate['key'], outcome, reason='Stale', manifest=manifest['rid'])
            assert not growth_plan.completed(VAULT, candidate['key'], growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
            assert candidate['key'] not in {c['key'] for c in growth_plan.plan(VAULT)['candidates']}
        """)

    def test_process_exit_alone_is_not_receipt(self):
        self.check_case("""
            node('A')
            for code in (0, 7):
                result = growth.run(VAULT, [sys.executable, '-c', 'import sys; sys.stdin.read(); sys.exit(' + str(code) + ')'])
                assert not result['ok'], result
                assert result['returncode'] == code
                assert set(result['outcomes'].values()) == {'pending'}
                assert growth_plan.plan(VAULT)['candidates']
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r.get('kind') == 'review']
        """)

    def test_worker_receipt_and_deferred_next_run(self):
        self.check_case("""
            node('A')
            worker = "import sys; from osk import core,growth, growth_ledger, growth_plan, growth_reviews, worker_process; from osk.vault import Vault; VAULT = Vault.open(); assert sys.stdin.read(); plans=[r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r.get('kind')=='plan']; [growth_reviews.review(VAULT, c['key'], 'deferred', reason='Need more evidence',manifest=plans[-1]['rid']) for c in plans[-1]['candidates']]"
            result = growth.run(VAULT, [sys.executable, '-c', worker])
            assert not result['ok'], result
            assert growth_plan.plan(VAULT)['candidates']
            worker = worker.replace("'deferred'", "'no_value'")
            worker += "; from osk import organization; [organization.review(VAULT, j['key'],j['scope'],'complete','The fixture group remains coherent.',after=organization.snapshot(VAULT, j['scope'])['snapshot'],checked=[{'unit':u['unit'],'reason':'Known fixture claim'} for u in j['review_units']]) for j in plans[-1]['organization_jobs']]"
            result = growth.run(VAULT, [sys.executable, '-c', worker])
            assert result['ok'], result
            assert set(result['outcomes'].values()) == {'no_value'}
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
        """)

    def test_preserved_requires_current_distillation_evidence(self):
        self.check_case("""
            node('A')
            manifest = register()
            candidate = manifest['candidates'][0]
            try:
                growth_reviews.review(VAULT, candidate['key'], 'preserved', target='Invented', reason='A process said success',manifest=manifest['rid'])
                raise AssertionError('unverified preservation accepted')
            except ValueError:
                pass
            assert growth_plan.plan(VAULT)['candidates']
        """)

    def test_useful_subset_does_not_create_false_evidence(self):
        self.check_case("""
            from osk import distillation, contract
            for name in ('A','B','Noise','Other'):
                node(name)
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='00_Domain/Principles')
            try:
                write.create_node(VAULT, **request)  # the user-only confirmation gate remains intact
            except write.WriteError:
                pass
            assert write.create_node(VAULT, **request)['ok']
            manifest = register(growth_plan.plan(VAULT, 1))
            candidate = manifest['candidates'][0]
            selected = [s for s in candidate['sources'] if s['name'] in {'A','B'}]
            result = distillation.create_node(VAULT, 
                {'key':candidate['key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in selected],'hub':'Principles'},
                title='Shared rule',summary='Rule from A and B',body='A and B support this rule, with limited scope.',
                drafter='gpt-6-astra',space='00_Domain/Principles')
            assert result['ok'], result
            receipt = growth_reviews.review(VAULT, candidate['key'],'preserved',target='Shared rule',
                reason='A and B support the rule; Noise and Other are unrelated and omitted.',manifest=manifest['rid'])
            actual = contract.parse(VAULT.root / '00_Domain/Principles/Shared rule.md').edges('derived-from')
            assert set(actual) == {s['name'] for s in selected}, actual
            assert len(receipt['omitted_sources']) == 2
            assert candidate['key'] not in {c['key'] for c in growth_plan.plan(VAULT, 20)['candidates']}
            distillation._job_path(VAULT, candidate['key']).unlink()
            assert candidate['key'] not in {c['key'] for c in growth_plan.plan(VAULT, 20)['candidates']}, 'shared receipt depended on local journal'
            write.update_node(VAULT, 'Shared rule',old_text='A and B support this rule, with limited scope.',new_text='Changed after completion.')
            assert candidate['key'] in {c['key'] for c in growth_plan.plan(VAULT, 20)['candidates']}
        """)

    def test_previous_saved_output_is_available_without_duplicate_node(self):
        self.check_case("""
            from osk import distillation
            node('A')
            node('B')
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='00_Domain/Principles')
            try:
                write.create_node(VAULT, **request)
            except write.WriteError:
                pass
            assert write.create_node(VAULT, **request)['ok']
            growth.run(VAULT, [sys.executable,'-c','import sys; sys.stdin.read()'],limit=1)
            first = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind'] == 'plan'][-1]
            candidate = first['candidates'][0]
            result = distillation.create_node(VAULT, 
                {'key':candidate['distill_key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in candidate['sources']],'hub':'Principles'},
                title='Retained rule',summary='Previously written rule',body='A and B support this bounded rule.',
                drafter='gpt-6-astra',space='00_Domain/Principles')
            assert result['ok'], result
            next_candidate = growth_plan.plan(VAULT, 1)['candidates'][0]
            assert next_candidate['previous_distillations'][0]['key'] == candidate['distill_key']
            worker = "import sys; from osk import core,growth,organization, growth_ledger, growth_plan, growth_reviews, worker_process; from osk.vault import Vault; VAULT = Vault.open(); sys.stdin.read(); p=[r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]; [growth_reviews.review(VAULT, c['key'],'preserved',target='Retained rule',reason='Existing saved result still covers A and B.',manifest=p['rid']) for c in p['candidates']]; [organization.review(VAULT, j['key'],j['scope'],'complete','Sources and local hub read; one coherent group.',after=organization.snapshot(VAULT, j['scope'])['snapshot'],checked=[{'unit':u['unit'],'reason':'Known fixture claim'} for u in j['review_units']]) for j in p['organization_jobs']]"
            outcome = growth.run(VAULT, [sys.executable,'-c',worker],limit=2)
            assert outcome['ok'], outcome
            assert len([1 for p,k in graph.Index(VAULT).nodes.values() if k[0]=='domain' and not placement.is_hub(p)]) == 1
        """)

    def test_organization_packet_without_snapshot_refuses_a_unit_changed_after_reading(self):
        self.check_case("""
            node('A')
            assert 'Use osk MCP tools only' in growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
            assert 'growth checkpoint --file' not in growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
            # The worker has no CLI snapshot. A unit it read that changes before the
            # runner applies the packet no longer matches its content key.
            change = ("from osk import write, cluster_gate, node_checks, node_moves, nodefile; n=VAULT.root/'00_Scope/W1/A.md'; "
                      "assert write.update_node(VAULT, 'A',body='Changed after the worker read it.',expect_hash=core.sha256_file(n))['ok']")
            stale = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=3)
            assert any('changed review unit' in e for e in stale['final_reviews']['errors']), stale
            assert {s['status'] for s in stale['organization_outcomes'].values()} != {'complete'}, stale
            fresh = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
            assert fresh['ok'], fresh
            assert {s['status'] for s in fresh['organization_outcomes'].values()} == {'complete'}, fresh
        """)

    def test_organization_packet_is_held_to_the_snapshot_in_its_plan(self):
        self.check_case("""
            node('A')
            node('B')
            # Another session swaps A's evidence after the plan. Unit keys cover bodies only,
            # so the planned snapshot is what tells the reviewed state from the current one.
            change = "from osk import write, cluster_gate, node_checks, node_moves, nodefile; assert write.update_node(VAULT, 'A',add_edges={'derived-from':'B'})['ok']"
            stale = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=3)
            assert any('changed after inspection' in e for e in stale['final_reviews']['errors']), stale
            assert {s['status'] for s in stale['organization_outcomes'].values()} != {'complete'}, stale
            fresh = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
            assert fresh['ok'], fresh
            assert {s['status'] for s in fresh['organization_outcomes'].values()} == {'complete'}, fresh
        """)

    def test_organization_packet_keeps_units_read_before_its_own_edit(self):
        self.check_case("""
            node('A')
            node('B')
            # The worker reads A, corrects B and checkpoints A as deferred. Its own write moves
            # the scope past the planned snapshot; A's unchanged unit is still its progress and
            # B goes to another reviewer (#136 M4: the packet was refused and nothing was kept).
            change = ("from osk import write, cluster_gate, node_checks, node_moves, nodefile; n=VAULT.root/'00_Scope/W1/B.md'; "
                      "assert write.update_node(VAULT, 'B',body='Corrected by the worker.',expect_hash=core.sha256_file(n))['ok']")
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change, org='deferred', read="u['name']=='A'")],
                                limit=3)
            # B is also a Domain candidate's source, so that candidate is refused; only the
            # organization checkpoint is under test here.
            errors = result['final_reviews']['errors']
            assert not [e for e in errors if e.startswith('Organization')], errors
            assert {s['status'] for s in result['organization_outcomes'].values()} != {'complete'}, result
            from osk import organization
            nxt = organization.plan(VAULT, 'W1')
            assert not {u['name'] for u in nxt.get('review_units', [])} & {'A', 'B'}, nxt
            assert nxt.get('handoff') or nxt.get('coverage', {}).get('handoff'), nxt
        """)
        self.check_case("""
            node('A')
            node('B')
            # The worker also lists B, which it then corrected. B's judgment was of the old body,
            # so only A is kept; B is named back and goes to another reviewer.
            change = ("from osk import write, cluster_gate, node_checks, node_moves, nodefile; n=VAULT.root/'00_Scope/W1/B.md'; "
                      "assert write.update_node(VAULT, 'B',body='Corrected by the worker.',expect_hash=core.sha256_file(n))['ok']")
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change, org='deferred')], limit=3)
            errors = result['final_reviews']['errors']
            assert not [e for e in errors if e.startswith('Organization')], errors
            assert any('changed after reading' in n for n in result['final_reviews']['notes']), result
            from osk import organization
            nxt = organization.plan(VAULT, 'W1')
            assert not {u['name'] for u in nxt.get('review_units', [])} & {'A', 'B'}, nxt
        """)

    def test_final_packet_applies_only_observed_domain_preservation(self):
        self.check_case("""
            node('A')
            node('B')
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='00_Domain/Principles')
            try:
                write.create_node(VAULT, **request)
            except write.WriteError:
                pass
            assert write.create_node(VAULT, **request)['ok']
            change = "c=p['candidates'][0]; result=distillation.create_node(VAULT, {'key':c['distill_key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in c['sources']],'hub':'Principles'},title='Observed rule',summary='Rule from A and B',body='A and B support this bounded rule.',drafter='gpt-6-astra',space='00_Domain/Principles'); assert result['ok'],result; q['osk_reviews']['domain'][0].update(outcome='preserved',target='Observed rule',reason='A and B support this bounded rule.')"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=2)
            assert result['ok'], result
            assert set(result['domain_outcomes'].values()) == {'preserved'}, result
            assert result['final_reviews']['state'] == 'applied', result
            # Newly created Domain knowledge is pending its own organization
            # review; the Scope review cannot acknowledge it implicitly.
            pending = growth_plan.plan(VAULT)['organization_jobs']
            assert [j['scope'] for j in pending] == ['00_Domain/Principles'], pending
            reviewed = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=1)
            assert reviewed['ok'], reviewed
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
        """)

    def test_final_provider_messages_accept_codex_and_claude(self):
        self.check_case("""
            for wrapper in ('codex','claude','claude_trailing','claude_two_turns'):
                node(wrapper)
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker(wrapper=wrapper)],limit=1)
                assert result['ok'], result
                assert result['final_reviews']['state'] == 'applied', result
        """)

    def test_tool_output_unfinished_and_failed_provider_packets_are_ignored(self):
        self.check_case("""
            node('A')
            for wrapper in ('codex_tool','codex_no_complete','codex_late_error','claude_tool','claude_error','claude_continued'):
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker(wrapper=wrapper)])
                assert not result['ok'], (wrapper,result)
                assert result['final_reviews']['state'] == 'rejected', (wrapper,result)
                assert set(result['domain_outcomes'].values()) == {'pending'}, result
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(code=7)])
            assert not result['ok'] and result['final_reviews']['state'] == 'not_applied', result
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
        """)

    def test_a_wrong_packet_is_refused_whole_and_a_wrong_entry_only_itself(self):
        self.check_case("""
            node('A')
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker("q['osk_reviews']['manifest']='old-manifest'")])
            assert not result['ok'] and result['final_reviews']['state'] == 'rejected', result
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
        """)
        for change, kept in (
                ("q['osk_reviews']['domain'][0]['key']='unselected'", False),
                ("q['osk_reviews']['domain'].append(q['osk_reviews']['domain'][0])", False),
                ("q['osk_reviews']['domain'][0]['command']='must never execute'", False),
                ("q['osk_reviews']['scope']=[{'harness':'claude','conversation_id':'unknown',"
                 "'through':'unknown','outcome':'no_value','reason':'Spoof'}]", True)):
            # The packet is right and one entry is not: only that entry is set aside with its
            # reason, and the other decisions in the packet still apply (헌법 1조 3항).
            self.check_case(f"""
                node('A')
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker({change!r})])
                assert not result['ok'] and result['final_reviews']['state'] == 'incomplete', result
                assert result['final_reviews']['errors'], result
                assert {{s['status'] for s in result['organization_outcomes'].values()}} == {{'complete'}}, result
                reviews = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
                assert bool(reviews) == {kept}, reviews
            """)
        # A decision with a needless part (a target on no_value) keeps its meaning; the part is
        # dropped and named in the notes.
        self.check_case("""
            node('A')
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker("q['osk_reviews']['domain'][0]['target']='Whatever'")])
            assert result['ok'], result
            assert any('target was dropped' in n for n in result['final_reviews']['notes']), result
            assert [r['outcome'] for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review'] == ['no_value']
        """)

    def test_a_report_from_an_older_selection_does_not_undo_a_newer_one(self):
        self.check_case("""
            node('A')
            old, new = register(), register()        # two selections of the same comparison
            key = old['candidates'][0]['key']
            assert key == new['candidates'][0]['key']
            growth_reviews.review(VAULT, key, 'no_value', reason='Decided on the later selection.', manifest=new['rid'])
            assert growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
            # The earlier selection's worker reports late. Its report is kept, but it does not
            # undo the later selection's decision (the growth ledger is causal, so a late append
            # would otherwise become the latest).
            growth_reviews.review(VAULT, key, 'deferred', reason='Late report from the earlier selection.', manifest=old['rid'])
            assert growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT)), 'an older report reopened it'
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 2
            # The same selection can still reopen its own decision on purpose.
            growth_reviews.review(VAULT, key, 'deferred', reason='Reopened within the later selection.', manifest=new['rid'])
            assert not growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
        """)

    def test_selections_made_apart_stay_undecided_until_a_later_one(self):
        self.check_case("""
            node('A')
            # Two devices select the same comparison before syncing. Neither selection follows
            # the other, so their different reports leave it undecided (Mechanism §3 1) instead
            # of letting the larger RID win. A selection made after the merge decides it.
            base = growth_ledger.ledger_path(VAULT).read_bytes() if growth_ledger.ledger_path(VAULT).exists() else b''
            x = register()
            key = x['candidates'][0]['key']
            growth_reviews.review(VAULT, key, 'deferred', reason='Device X could not decide.', manifest=x['rid'])
            branch = growth_ledger.ledger_path(VAULT).read_bytes()[len(base):]
            growth_ledger.ledger_path(VAULT).write_bytes(base)
            y = register()
            assert y['candidates'][0]['key'] == key and core.rid_key(x['rid']) < core.rid_key(y['rid'])
            growth_reviews.review(VAULT, key, 'no_value', reason='Device Y found nothing to keep.', manifest=y['rid'])
            growth_ledger.ledger_path(VAULT).write_bytes(growth_ledger.ledger_path(VAULT).read_bytes() + branch)    # the sync merge
            rows = growth_ledger.ledger_records(VAULT)
            assert growth_ledger.latest_report(rows, key, 'key') is None
            assert not growth_plan.completed(VAULT, key, rows, graph.Index(VAULT)), 'the larger RID decided a fork'
            z = register()
            growth_reviews.review(VAULT, key, 'no_value', reason='Decided after the merge.', manifest=z['rid'])
            assert growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
        """)

    def test_a_runner_stopped_while_applying_keeps_what_it_applied(self):
        self.check_case("""
            from unittest.mock import patch
            node('A')
            node('B', 'W2')
            # M2: the runner dies between two decisions of one final packet. The decision it
            # applied stays complete, nothing records the run as done, and the next plan presents
            # only the selection whose decision was not applied.
            class Crash(BaseException):
                pass
            real, calls = growth_reviews.review, []
            def review(*a, **kw):
                calls.append(kw.get('key', a[0] if a else None))
                if len(calls) == 2:
                    raise Crash()
                return real(*a, **kw)
            with patch.object(growth_reviews, 'review', side_effect=review):
                try:
                    growth.run(VAULT, [sys.executable,'-c',packet_worker()], limit=3)
                    raise AssertionError('the run went on after the runner died')
                except Crash:
                    pass
            rows = core.ledger_read(growth_ledger.ledger_path(VAULT))
            reviews = [r for r in rows if r['kind']=='review']
            assert [r['key'] for r in reviews] == calls[:1], (reviews, calls)
            assert not [r for r in rows if r['kind']=='run']
            assert growth_plan.completed(VAULT, calls[0], growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
            keys = [c['key'] for c in growth_plan.plan(VAULT, 3)['candidates']]
            assert calls[0] not in keys and calls[1] in keys, (keys, calls)
        """)

    def test_a_runner_stopped_after_applying_does_not_present_its_decisions_again(self):
        self.check_case("""
            from unittest.mock import patch
            node('A')
            # M2: the decisions are applied, then the runner dies before it records the run and
            # replies. Nothing applied is undone or presented again.
            class Crash(BaseException):
                pass
            real = core.ledger_append
            def append(path, record, *a, **kw):
                if record.get('kind') == 'run':
                    raise Crash()
                return real(path, record, *a, **kw)
            with patch.object(core, 'ledger_append', side_effect=append):
                try:
                    growth.run(VAULT, [sys.executable,'-c',packet_worker()], limit=3)
                    raise AssertionError('the run went on after the runner died')
                except Crash:
                    pass
            rows = core.ledger_read(growth_ledger.ledger_path(VAULT))
            assert [r for r in rows if r['kind']=='review'] and not [r for r in rows if r['kind']=='run'], rows
            again = growth_plan.plan(VAULT, 3)
            assert not again['candidates'], again['candidates']
            assert not again['organization_jobs'], again['organization_jobs']
        """)

    def test_final_packet_saved_declaration_requires_actual_receipt(self):
        self.check_case("""
            node('A')
            change = "q['osk_reviews']['domain'][0].update(outcome='preserved',target='Missing rule')"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)])
            assert not result['ok'] and result['final_reviews']['state'] == 'incomplete', result
            assert result['final_reviews']['errors'], result
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
        """)

    def test_final_packet_does_not_duplicate_existing_valid_review(self):
        self.check_case("""
            node('A')
            node('B','W2')
            change = "growth_reviews.review(VAULT, **q['osk_reviews']['domain'][0],manifest=p['rid'])"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=3)
            assert result['ok'], result
            assert 'already_complete' in result['final_reviews']['domain'].values(), result
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == result['domain_selected'] == 2
        """)
