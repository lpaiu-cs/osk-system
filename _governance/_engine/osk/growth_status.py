"""Read-only growth evidence: inventory now, recorded decisions in a time window.

Never call integration.status/list_pending: those write state (they close repair
entries left by the pre-4.2 receipt recheck). A report is an observation, not an
acknowledgement or a semantic growth verdict.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json

from . import core, epoch, growth, integration, organization, rechecks, evictions


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('timestamps must include a timezone')
    return result


def _window(rows: list[dict], since: datetime | None, until: datetime) -> dict:
    selected, outside, undated = [], 0, []
    for row in rows:
        try:
            at = _time(row['at'])
        except (ValueError, KeyError, TypeError):
            undated.append(row.get('rid') or row.get('through'))
            continue
        if (since is None or since <= at) and at < until:
            selected.append(row)
        else:
            outside += 1
    return {'rows': selected, 'outside_window': outside, 'unknown_time': undated}


def _states() -> list[dict]:
    probe = integration.state_path('claude', 'inventory')
    prefix = '-'.join(probe.name.split('-')[:3]) + '-'
    states = []
    # glob can suppress directory access errors and report a false empty inventory.
    for path in sorted(p for p in probe.parent.iterdir() if p.match(prefix + '*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if path != integration.state_path(data['harness'], data['conversation_id']):
            raise ValueError(f'integration state filename identity mismatch: {path.name}')
        states.append(integration._validate_state(data, data['harness'], data['conversation_id']))
    return states


def _integration(states: list[dict]) -> dict:
    items = []
    for state in states:
        view = integration._view(state)
        refs = {r['ref'] for r in state['rounds']}
        repairs = {ref for job in state.get('repair_pending', {}).values() for ref in job['refs']}
        if repairs - refs:
            raise ValueError('repair references are outside the captured conversation')
        items.append({
            'harness': state['harness'], 'conversation_id': state['conversation_id'],
            'captured_rounds': view['captured_rounds'],
            'unreviewed_rounds': view['captured_rounds'] - view['reviewed_rounds'],
            'repair_jobs': len(state.get('repair_pending', {})), 'repair_rounds': len(repairs),
            'review_pending_rounds': len(set(view['pending_refs'])),
            'capture_pending': view['capture_pending'], 'capture_error': view['capture_error'],
            'capture_recovery': view['capture_recovery'],
            'last_route': view['response_growth_route'],
            'last_fork_result': view['response_growth'].get('last_result'),
            'round_endings': dict(Counter(r.get('completion', 'unknown') for r in state['rounds'])),
        })
    totals = {key: sum(item[key] for item in items) for key in (
        'captured_rounds', 'unreviewed_rounds', 'repair_jobs', 'repair_rounds', 'review_pending_rounds')}
    return {**totals, 'conversations': len(items),
            'conversations_with_unreviewed_rounds': sum(i['unreviewed_rounds'] > 0 for i in items),
            'conversations_with_review_pending': sum(i['review_pending_rounds'] > 0 for i in items),
            'capture_pending_conversations': sum(bool(i['capture_pending']) for i in items),
            'capture_error_conversations': sum(bool(i['capture_error']) for i in items),
            'capture_recovery_states': dict(Counter(i['capture_recovery']['state'] for i in items
                                                    if i['capture_pending'] or i['capture_error'])),
            'capture_failure_phases': dict(Counter(i['capture_recovery']['phase'] for i in items
                                                   if i['capture_error'])),
            'uncaptured_rounds': None,
            'basis': 'stored cursors; legacy missing-source advice may locate a file, but native tails are not parsed or counted as reviewed',
            'items': items}


def _invocation(plan: dict) -> str:
    declared = plan.get('invocation')
    if declared in {'manual', 'user_request', 'scheduled', 'stop_hook', 'unknown'}:
        return declared
    if str(plan.get('work_context', '')).startswith('stop:'):
        return 'legacy_stop_context'
    return 'unknown'  # `daily` is a work queue, not proof of a scheduler invocation.


def _runs(rows: list[dict], since: datetime | None, until: datetime) -> dict:
    plans = {r['rid']: r for r in rows if r.get('kind') == 'plan'}
    window = _window([r for r in rows if r.get('kind') == 'run'], since, until)
    items = []
    for row in window.pop('rows'):
        plan = plans.get(row.get('manifest'), {})
        queues = {}
        for name in ('domain', 'scope', 'organization', 'eviction', 'recheck'):
            outcomes = row.get(name + '_outcomes', row.get('outcomes', {}) if name == 'domain' else {})
            if name == 'eviction':
                # Settlement can precede review or survive a later deferral.
                queues[name] = {
                    'by_status': dict(Counter(o.get('status', 'unknown') for o in outcomes.values())),
                    'by_review': dict(Counter((o.get('review') or {}).get('outcome', 'unreviewed')
                                              for o in outcomes.values())),
                    'by_settlement': dict(Counter((o.get('settlement') or {}).get('outcome', 'unsettled')
                                                  for o in outcomes.values())),
                }
                continue
            counts = Counter()
            for outcome in outcomes.values():
                if isinstance(outcome, dict):
                    # A closed task may mean removed basis, disappearance, or escalation.
                    reason = outcome.get('reason')
                    disposition = {'node gone': 'node_gone', 'basis removed': 'basis_removed',
                                   'basis dangling': 'basis_dangling',
                                   'escalated to the user': 'escalated'}.get(reason)
                    label = disposition or outcome.get('outcome') or outcome.get('status', 'unknown')
                else:
                    label = str(outcome)
                counts[label] += 1
            queues[name] = dict(counts)
        selected = row.get('selected')
        items.append({'rid': row['rid'], 'manifest': row.get('manifest'), 'at': row['at'],
                      'invocation': _invocation(plan), 'work_context': plan.get('work_context'),
                      'engine_rev': plan.get('engine_rev'), 'selected': selected,
                      'recorded_ok': row.get('ok'), 'state': row.get('state'),
                      'returncode': row.get('returncode'), 'error': row.get('error'),
                      'final_state': row.get('final_reviews', {}).get('state'), 'queues': queues})
    results = {r.get('manifest') for r in rows if r.get('kind') == 'run'}
    unfinished = _window([p for key, p in plans.items() if key not in results], since, until)
    return {**window, 'runs': len(items),
            'by_invocation': dict(Counter(i['invocation'] for i in items)),
            'native_exit_zero': sum(i['returncode'] == 0 for i in items),
            'recorded_nonempty_complete': sum(i['recorded_ok'] is True and isinstance(i['selected'], int)
                                              and i['selected'] > 0 for i in items),
            'empty_selection': sum(i['selected'] == 0 for i in items),
            'unfinished_plan_ids': [p['rid'] for p in unfinished['rows']],
            'unfinished_unknown_time': unfinished['unknown_time'],
            'user_intent': 'not inferred from entrypoint; a Stop may follow an explicit preservation request',
            'items': items}


def _recheck_history(rows: list[dict], since: datetime | None, until: datetime) -> dict:
    window = _window(rows, since, until)
    counts, items = Counter(), []
    for row in window.pop('rows'):
        result, reason = row.get('result'), row.get('reason')
        if result == 'bound':
            label = 'baseline' if reason == rechecks.BASELINE else 'binding'
        elif result == 'unchanged':
            label = 'carried' if reason == rechecks.CARRIED else 'explicit_unchanged'
        elif result == 'updated':
            label = 'explicit_updated'
        else:
            label = 'unknown'
        counts[label] += 1
        items.append({'rid': row['rid'], 'category': label})
    return {**window, 'records': len(items), 'by_category': dict(counts), 'items': items}


def _organization(idx) -> dict:
    scopes = {k[1] if k[0] == 'scope' else core.DOMAIN + '/' + k[1]
              for _, k in idx.nodes.values() if k[0] in {'scope', 'domain'}}
    scopes.update(p['scope'] for p in organization._load()['plans'].values())
    items = []
    for scope in sorted(scopes):
        for job in organization.pending([scope], limit=1, idx=idx, record=False):
            items.append({'scope': scope, **job['coverage'],
                          'wiring_issues': len(job['issues']),
                          'reference_issues': len(job['references']),
                          'pending_moves': len(job['pending_moves'])})
    return {'pending_scopes': len(items), 'remaining_units': sum(i['remaining'] for i in items),
            'units_in_pending_scopes': sum(i['total'] for i in items), 'items': items,
            'basis': 'current body ranges and structural receipts; not semantic verification'}


def _preservation(receipts: list[dict], idx) -> dict:
    from . import distillation
    unique = {json.dumps(r, sort_keys=True): r for r in receipts}
    items = [{'key': r['key'], 'target_id': r['target'].get('id'),
              'structure': distillation.structure(r, idx)} for r in unique.values()]
    return {'receipt_versions': len(items),
            'structure': dict(Counter('broken' if i['structure'] else 'intact' for i in items)),
            'items': items,
            'basis': 'distinct saved receipts in the history window; after acknowledgement only the target, '
                     'its top-level cluster and its derived-from sources are checked, and a break is reported, '
                     'not reopened; not new-node counts'}


def report(*, since: str | None = None, until: str | None = None, preflight: bool = False) -> dict:
    started = core.now_iso()
    begin, end = _time(since) if since else None, _time(until or started)
    if begin is not None and begin >= end:
        raise ValueError('since must be earlier than until')
    out = {'ok': True, 'schema': 'osk-growth-status/1', 'root': str(core.ROOT),
           'window': {'since': since, 'until': end.isoformat(), 'bounds': '[since, until)'},
           'observation': {'started_at': started, 'atomic': False},
           'meaning': {'ok': 'report sections were read, not growth success',
                       'semantic_growth': 'not_measured', 'downstream_reuse': 'not_measured',
                       'autonomy': 'not_inferred'}, 'errors': []}

    def section(name, read):
        try:
            out[name] = read()
            return out[name]
        except (ValueError, OSError, KeyError, TypeError, AttributeError, epoch.EpochError) as exc:
            out['ok'] = False
            out[name] = None
            out['errors'].append({'section': name, 'error': f'{type(exc).__name__}: {exc}'})
            return None

    def runtime():
        from . import update
        disk = epoch.on_disk()
        manifest = core.ROOT / 'release.json'
        return {'observer': 'this CLI process, not the connected MCP server',
                'engine_path': str(epoch.ENGINE_ROOT), 'engine_rev': epoch.loaded(),
                'engine_disk_rev': disk, 'engine_stale': None if epoch.loaded() == epoch.UNKNOWN else epoch.loaded() != disk,
                'applied_version': update.current_version(),
                'manifest_version': json.loads(manifest.read_text(encoding='utf-8')).get('version') if manifest.exists() else None,
                'mcp': 'not_observed; pair with overview engine_rev, engine_disk_rev and engine_observed_at',
                'cli_preflight': None}

    runtime_state = section('runtime', runtime)
    if preflight and runtime_state is not None:
        from . import response_growth, harness
        runtime_state['cli_preflight'] = section('cli_preflight', lambda: [
            response_growth.doctor(h) for h in harness.fork_names()])
        out.pop('cli_preflight', None)
    saved, chosen, history = [], [], []
    def integration_inventory():
        saved.extend(_states())
        return _integration(saved)
    states = section('integration', integration_inventory)
    def review_history():
        reviews = _window([{**r, 'harness': s['harness'], 'conversation_id': s['conversation_id']}
                           for s in saved for r in s['reviews']], begin, end)
        chosen.extend(reviews.pop('rows'))
        return {**reviews, 'records': len(chosen),
                'outcomes': dict(Counter(r.get('outcome', 'unknown') for r in chosen)),
                'items': [{k: r.get(k) for k in ('harness', 'conversation_id', 'through', 'at', 'outcome')}
                          for r in chosen]}
    if states is not None:
        section('reviews', review_history)
    else:
        out['reviews'] = None
    def run_history():
        history.extend(growth._records())
        return _runs(history, begin, end)
    section('runs', run_history)
    def recheck_records():
        records, valid = rechecks._read()
        if not valid:
            raise ValueError('recheck ledger damaged')
        return _recheck_history(records, begin, end)
    section('recheck_history', recheck_records)
    def eviction_inventory():
        records = evictions.records()
        pending = evictions.unsettled(recs=records)
        return {'pending_entries': len(pending),
                'oldest_days': max((evictions.age_days(r) for r in pending), default=0),
                'pending_ids': [r['rid'] for r in pending],
                'settlement_records': dict(Counter(r['outcome'] for r in records if r.get('kind') == 'settle'))}
    section('evictions', eviction_inventory)
    try:
        idx = growth._index()
    except (ValueError, OSError) as exc:
        out['ok'] = False
        out['errors'].append({'section': 'graph', 'error': str(exc)})
        out.update(organization=None, rechecks=None, preservation=None)
    else:
        section('organization', lambda: _organization(idx))
        def current_rechecks():
            items, baseline = rechecks.candidates(idx)
            return {'baseline_pending': baseline, 'candidate_pairs': len(items),
                    'awaiting_agent': sum('escalated' not in i for i in items),
                    'escalated': sum('escalated' in i for i in items)}
        section('rechecks', current_rechecks)
        def preservation():
            receipts = [p for r in chosen if r.get('outcome') == 'preserved' for p in r['receipts']]
            # Domain review records hold synchronized receipts too.
            if out['runs'] is None or out['reviews'] is None:
                raise ValueError('receipt history is incomplete')
            domain = _window([r for r in history if r.get('kind') == 'review'], begin, end)
            receipts += [r['distillation'] for r in domain['rows'] if r.get('outcome') == 'preserved']
            return _preservation(receipts, idx)
        section('preservation', preservation)
    out['observation']['finished_at'] = core.now_iso()
    return out
