"""Summarize saved v9 runs; separate action agreement from engine and semantic checks."""
from collections import Counter
import difflib
import html
import json
from pathlib import Path
import statistics
import sys

BOUNDARY = set('F03 F14 F19 F20 F24 F27 F29'.split())
CLEANUP = set('F02 F04 F08 F13 F26 F31'.split())
LABELS = {'valid_preserve':'보존 적절', 'disputed_preserve':'판정 경계',
          'boundary_preserve':'경계·보존', 'move_date_unverified':'이동·날짜 미확정',
          'unnecessary_deletion':'불필요한 삭제', 'wrong_delete_nonnode':'삭제 규칙 위반',
          'held_missing_date':'보류·날짜 미확인', 'held_date_and_relation':'정리 보류',
          'valid_preserve_with_hold':'보존·부분 보류', 'valid_move_date_inferred':'내용 보존 이동·날짜 추정',
          'valid_move':'이동 적절', 'disputed_move':'의미 보존·판정 경계'}


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def collect(root):
    annotations = read(root / 'semantic-review.json', {})
    rows = []
    for done in sorted((root / 'runs').glob('*/done.json')):
        p = done.parent
        meta = read(done)
        plan = read(p / 'planned.json')
        job = plan['recheck_jobs'][0]
        try:
            packet = read(p / 'last.txt', {})
        except ValueError:
            packet = {}
        entries = packet.get('osk_reviews', {}).get('abstraction', [])
        before, after = [(p / f).read_text(encoding='utf-8') for f in ('before.md', 'after.md')]
        diff = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='before', tofile='after'))
        tools = Counter()
        errors = []
        cited_calls = []
        for line in (p / 'events.jsonl').read_text(encoding='utf-8').splitlines():
            event = json.loads(line)
            item = event.get('item', {})
            if event.get('type') == 'item.completed' and item.get('type') == 'mcp_tool_call':
                tools[item['tool']] += 1
                payloads = []
                for block in (item.get('result') or {}).get('content', []):
                    if block.get('type') == 'text':
                        try:
                            payloads.append(json.loads(block['text']))
                        except ValueError:
                            pass
                failed = bool(item.get('error') or item.get('status') == 'failed'
                              or (item.get('result') or {}).get('isError')
                              or any(isinstance(p, dict) and (p.get('ok') is False or p.get('error'))
                                     for p in payloads))
                if failed:
                    errors.append(item)
                if item['tool'] == 'read_cited':
                    cited_calls.append({'arguments': item.get('arguments'), 'failed': failed,
                                        'results': payloads})
        fid = p.name.split('_')[0]
        expected = 'boundary' if fid in BOUNDARY else 'cleanup' if fid in CLEANUP else 'preserve'
        acted = any(e['outcome'] in ('deleted', 'moved') for e in entries) and before != after
        agreed = None if expected == 'boundary' else acted if expected == 'cleanup' else before == after
        normal_input = {k: v for k, v in plan.items() if k != 'manifest'}
        import hashlib
        raw_engine = read(p / 'verification.json')
        corrected_engine = read(p / 'verification-corrected.json', raw_engine)
        rows.append(dict(run=p.name, fid=fid, model=meta['model'], repeat=meta['repeat'],
                         seconds=meta['seconds'], rc=meta['rc'], usage=meta['usage'],
                         engine=corrected_engine, engine_raw=raw_engine,
                         tools=dict(tools), tool_errors=len(errors),
                         cited_calls=cited_calls,
                         prompt_chars=len((p / 'prompt.txt').read_text(encoding='utf-8')),
                         expected=expected, action_agreement=agreed, entries=entries, diff=diff,
                         original_diff=job['abstraction']['commits'][0]['diff'],
                         sources=job['abstraction']['cited'],
                         semantic=annotations.get(p.name),
                         input_hash=hashlib.sha256(json.dumps(normal_input,ensure_ascii=False,sort_keys=True).encode()).hexdigest()))
    return rows


def main():
    root = Path(sys.argv[1]).resolve()
    experiment = read(root / 'experiment.json', {'expected_runs':132,'repeats':2})
    rows = collect(root)
    summary = {}
    for model in sorted({r['model'] for r in rows}):
        rs = [r for r in rows if r['model'] == model]
        summary[model] = {'runs': len(rs), 'engine_complete': sum(r['engine']['complete'] for r in rs),
                          'engine_complete_raw': sum(r['engine_raw']['complete'] for r in rs),
                          'action_agreement': sum(r['action_agreement'] is True for r in rs),
                          'fixed_cases': sum(r['action_agreement'] is not None for r in rs),
                          'boundary_runs': sum(r['expected'] == 'boundary' for r in rs),
                          'seconds_total': round(sum(r['seconds'] for r in rs), 3),
                          'seconds_median': statistics.median(r['seconds'] for r in rs),
                          'read_cited_calls': sum(r['tools'].get('read_cited', 0) for r in rs),
                          'all_tool_calls': sum(sum(r['tools'].values()) for r in rs),
                          'tool_errors': sum(r['tool_errors'] for r in rs)}
        for k in ('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens'):
            summary[model][k] = sum(r['usage'].get(k,0) for r in rs)
        summary[model]['uncached_input_tokens'] = summary[model]['input_tokens'] - summary[model]['cached_input_tokens']
        summary[model]['semantic_review'] = dict(Counter(r['semantic']['status'] if r['semantic'] else 'pending'
                                                        for r in rs))
    mismatches = [fid for fid in sorted({r['fid'] for r in rows})
                  if len({r['input_hash'] for r in rows if r['fid'] == fid}) != 1]
    result = {'experiment':experiment, 'summary':summary, 'input_mismatches':mismatches, 'runs':rows}
    assert sum(s['runs'] for s in summary.values()) == len(rows)
    assert all(s['cached_input_tokens'] <= s['input_tokens'] for s in summary.values())
    assert all(r['action_agreement'] is None for r in rows if r['fid'] in BOUNDARY)
    (root / 'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    esc = html.escape
    def cell(s, key):
        if key == 'engine_complete' and s[key] != s['engine_complete_raw']:
            return str(s['engine_complete_raw'])+' → '+str(s[key])
        if key == 'seconds_median':
            return f'{s[key]:.1f}초'
        if key.endswith('_tokens'):
            return f'{s[key]:,}'
        return str(s[key])
    table = ''.join('<tr><th>'+esc(m)+'</th>'+''.join('<td>'+cell(s,k)+'</td>' for k in (
        'runs','engine_complete','action_agreement','fixed_cases','seconds_median','uncached_input_tokens','output_tokens','read_cited_calls'))+'</tr>'
        for m,s in summary.items())
    expected = experiment['expected_runs']
    repeats = experiment['repeats']
    progress = f'총 {expected}회 중 {len(rows)}회 완료. '+('정식 실행 완료.' if len(rows)==expected else '진행 중인 실행은 집계에 아직 포함하지 않았습니다.')
    highlights = ''.join('<li>'+esc(note)+'</li>' for note in read(root / 'highlights.json', []))
    cards = []
    for r in rows:
        text = '\n'.join(e['outcome']+': '+e['reason'] for e in r['entries'])
        mark = '경계 사례' if r['action_agreement'] is None else '행동 일치' if r['action_agreement'] else '행동 불일치'
        check = json.dumps({'original':r['engine_raw'],'revalidated':r['engine']}
                           if r['engine'] != r['engine_raw'] else r['engine'],ensure_ascii=False,indent=2)
        status = '완료' if r['engine']['complete'] else '미완료'
        semantic = r['semantic']['reason'] if r['semantic'] else '검토 대기'
        verdict = LABELS.get(r['semantic']['status'],r['semantic']['status']) if r['semantic'] else '판독 대기'
        calls = json.dumps(r['cited_calls'], ensure_ascii=False, indent=2)
        source_cards = ''.join('<details><summary>'+esc(k)+'</summary><pre>'+
                              esc(json.dumps(v, ensure_ascii=False, indent=2))+'</pre></details>'
                              for k,v in r['sources'].items())
        cards.append(f'<details data-model="{esc(r["model"])}" data-case="{r["fid"]}"><summary>{esc(r["run"])} · {esc(verdict)} · {mark} · {r["seconds"]:.1f}초 · 엔진 {status}</summary>'
                     f'<p>{esc(text)}</p><p>의미 판독: {esc(semantic)}</p>'
                     f'<h3>원래 변경</h3><pre>{esc(r["original_diff"])}</pre><h3>모델의 저장 변경</h3><pre>{esc(r["diff"] or "변경 없음")}</pre>'
                     f'<details><summary>모델에게 처음 제공한 출처 발췌</summary>{source_cards}</details>'
                     f'<details><summary>read_cited 호출 · {len(r["cited_calls"])}회</summary><pre>{esc(calls)}</pre></details>'
                     f'<details><summary>엔진 확인</summary><pre>{esc(check)}</pre></details></details>')
    overview = []
    for fid in sorted({r['fid'] for r in rows}):
        cells = []
        for model in ('gpt-5.6-sol', 'gpt-6.1-terra'):
            for repeat in range(1,repeats+1):
                found = [r for r in rows if (r['fid'],r['model'],r['repeat']) == (fid,model,repeat)]
                value = ' / '.join(e['outcome'] for e in found[0]['entries']) if found else '대기'
                if found and found[0]['semantic']:
                    value += ' · '+LABELS.get(found[0]['semantic']['status'],found[0]['semantic']['status'])
                cells.append(value)
        overview.append('<tr><th>'+fid+'</th>'+''.join('<td>'+esc(s)+'</td>' for s in cells)+'</tr>')
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8"><link rel="icon" href="data:,"><title>OSK v9 Sol · Terra 비교</title>
<style>body{font:16px/1.65 system-ui;background:#f5f6fa;color:#202638;max-width:1200px;margin:40px auto;padding:0 24px}h1{font-size:32px}table{border-collapse:collapse;background:white;width:100%;font-size:14px}th,td{padding:10px;border-bottom:1px solid #ddd;text-align:left}details{background:white;border:1px solid #d8dce8;padding:14px;margin:14px 0;border-radius:8px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.65 ui-monospace,monospace;background:#f0f2f7;padding:14px}button{padding:8px 16px;margin:12px 8px 12px 0}</style>
<h1>OSK v9 · Sol / Terra 비교</h1><p>동일한 v9 엔진·발췌·medium 설정. 33사례 × 2모델 × '''+str(repeats)+'''회.</p><p>'''+progress+'''</p>
<p><b>행동 일치는 정답률이 아닙니다.</b> 확정군의 보존/정리 방향만 대조합니다. 엔진 완료는 저장·기록 검사이고, 의미 보존·귀속은 별도 판독입니다. 경계군 7건은 분리했습니다.</p>
<p>엔진 완료에 화살표가 있으면 동일 출처의 전체 경로·축약 좌표를 구분한 검사 오탐의 초기 값 → 재검증 값입니다. 모델 결과와 저장 내용은 그대로 두고 검사만 다시 적용했습니다.</p>
<table><tr><th>모델</th><th>실행</th><th>엔진 완료</th><th>행동 일치</th><th>확정군 실행</th><th>시간 중앙값</th><th>비캐시 입력</th><th>출력 토큰</th><th>read_cited</th></tr>'''+table+'''</table>
<ul>'''+highlights+'''</ul>
<details><summary>사례별 행동 한눈에 보기</summary><table><tr><th>사례</th>'''+''.join(f'<th>{m} {i}</th>' for m in ('Sol','Terra') for i in range(1,repeats+1))+'''</tr>'''+''.join(overview)+'''</table></details>
<label>모델 <select id="model" onchange="filter()"><option value="">전체</option><option>gpt-5.6-sol</option><option>gpt-6.1-terra</option></select></label>
<label>사례 <select id="case" onchange="filter()"><option value="">전체</option>'''+''.join('<option>'+fid+'</option>' for fid in sorted({r['fid'] for r in rows}))+'''</select></label>
'''+''.join(cards)+'''<script>function filter(){const m=document.getElementById('model').value,f=document.getElementById('case').value;document.querySelectorAll('[data-model]').forEach(e=>e.hidden=!!((m&&e.dataset.model!==m)||(f&&e.dataset.case!==f)))}</script></html>'''
    (root / 'report.html').write_text(page,encoding='utf-8')
    print(json.dumps({'summary':summary,'input_mismatches':mismatches},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
