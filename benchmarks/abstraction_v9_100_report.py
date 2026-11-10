"""Mechanical-only report for the 100-case expansion. No semantic grading."""
from collections import Counter
import html
import json
from pathlib import Path
import statistics
import sys

from abstraction_v9_report import collect


def main():
    root = Path(sys.argv[1]).resolve()
    experiment = json.loads((root/'experiment.json').read_text(encoding='utf-8')) if (root/'experiment.json').exists() else {}
    version = experiment.get('prompt_version', 'v9')
    cohort = experiment.get('cohort_description', '기존 33개 + 추가 67개')
    rows = collect(root)
    for r in rows:
        for key in ('expected','action_agreement','semantic'):
            r.pop(key,None)
        checks = []
        for line in (root/'runs'/r['run']/'events.jsonl').read_text(encoding='utf-8').splitlines():
            e = json.loads(line)
            i = e.get('item',{})
            if e.get('type')=='item.completed' and i.get('type')=='mcp_tool_call' and i.get('tool')=='verify_abstraction':
                payload = []
                for b in (i.get('result') or {}).get('content',[]):
                    if b.get('type')=='text':
                        try:
                            payload.append(json.loads(b['text']))
                        except ValueError:
                            payload.append(b['text'])
                checks.append(dict(arguments=i.get('arguments'),results=payload,error=i.get('error')))
        r['verification_calls'] = checks
        r['verification_failed'] = sum(bool(c['error']) or any(isinstance(v,dict) and v.get('ok') is False for v in c['results']) for c in checks)
        r['verification_passed'] = sum(any(isinstance(v,dict) and v.get('ok') is True for v in c['results']) for c in checks)
        r['recovered'] = bool(r['verification_failed'] and r['verification_passed'] and r['engine'].get('complete'))
        applied = r['engine'].get('applied',{})
        r['abstraction_complete'] = bool(applied.get('abstraction') and not applied.get('errors'))
        r['verification_recovered'] = bool(r['verification_failed'] and r['verification_passed'] and r['abstraction_complete'])
    summary = {}
    for model in sorted({r['model'] for r in rows}):
        rr = [r for r in rows if r['model']==model]
        summary[model] = dict(runs=len(rr),normal_exit=sum(r['rc']==0 for r in rr),
            engine_complete=sum(r['engine'].get('complete',False) for r in rr),
            abstraction_complete=sum(r['abstraction_complete'] for r in rr),
            median_seconds=statistics.median(r['seconds'] for r in rr),seconds=sum(r['seconds'] for r in rr),
            input_tokens=sum(r['usage'].get('input_tokens',0) for r in rr),
            cached_input_tokens=sum(r['usage'].get('cached_input_tokens',0) for r in rr),
            output_tokens=sum(r['usage'].get('output_tokens',0) for r in rr),
            tool_calls=sum(sum(r['tools'].values()) for r in rr),tool_errors=sum(r['tool_errors'] for r in rr),
            verify_failed_runs=sum(r['verification_failed']>0 for r in rr),recovered_runs=sum(r['recovered'] for r in rr),
            verify_recovered_runs=sum(r['verification_recovered'] for r in rr),
            no_verify_runs=sum(not r['verification_calls'] for r in rr),
            outcomes=dict(Counter(e['outcome'] for r in rr for e in r['entries'])),
            judgments=dict(Counter(next(iter({e.get('judgment','missing') for e in r['entries']}), 'missing') for r in rr)))
        summary[model]['uncached_input_tokens']=summary[model]['input_tokens']-summary[model]['cached_input_tokens']
    data = dict(semantic_review='not_performed',prompt_version=version,expected_runs=200,completed_runs=len(rows),summary=summary,runs=rows)
    (root/'results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    esc = html.escape
    cards=[]
    for r in sorted(rows,key=lambda r:(int(r['fid'][1:]),r['model'])):
        blocks=[('검토 대상이 된 과거 변경',r['original_diff']),('이번 모델이 실제 저장한 변경',r['diff'] or '변경 없음'),
                ('모델의 최종 판단',json.dumps(r['entries'],ensure_ascii=False,indent=2)),
                ('실행 중 검증과 복구',json.dumps(r['verification_calls'],ensure_ascii=False,indent=2)),
                ('최종 기계 검사',json.dumps(r['engine'],ensure_ascii=False,indent=2)),
                ('제공된 출처 발췌',json.dumps(r['sources'],ensure_ascii=False,indent=2))]
        label='기계 검사 완료' if r['engine'].get('complete') else '추상화 검사 완료·후속 재검토 미완료' if r['abstraction_complete'] else '기계 검사 미완료'
        cards.append(f'<details class="case" data-fid="{r["fid"]}" data-model="{r["model"]}"><summary>{esc(r["run"])} · {label} · {r["seconds"]:.1f}초 · 의미 판독 보류</summary>'+''.join(f'<h3>{title}</h3><pre>{esc(text)}</pre>' for title,text in blocks)+'</details>')
    table='<table><tr><th>모델</th><th>실행</th><th>추상화 / 전체 기계 완료</th><th>시간 중앙값</th><th>비캐시 입력</th><th>출력</th><th>검증 오류 실행</th><th>추상화 검사 복구</th></tr>'
    for model,s in summary.items():
        table+=f'<tr><th>{model}</th><td>{s["runs"]}</td><td>{s["abstraction_complete"]} / {s["engine_complete"]}</td><td>{s["median_seconds"]:.1f}초</td><td>{s["uncached_input_tokens"]:,}</td><td>{s["output_tokens"]:,}</td><td>{s["verify_failed_runs"]}</td><td>{s["verify_recovered_runs"]}</td></tr>'
    table+='</table>'
    filters='<label>모델 <select id="model"><option value="">전체</option>'+''.join(f'<option>{m}</option>' for m in summary)+'</select></label> <label>사례 <select id="fid"><option value="">전체</option>'+''.join(f'<option>{fid}</option>' for fid in sorted({r['fid'] for r in rows},key=lambda f:int(f[1:])))+'</select></label>'
    pending = [r['run'] for r in rows if not r['engine'].get('complete')]
    notes = '<p>후속 작업까지 완료되지 않은 실행: '+esc(', '.join(pending) or '없음')+'. 원래 실행 결과를 유지하며 재실행으로 대체하지 않았습니다.</p>'
    page='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OSK v9 · 100개 변경</title><style>
body{font:16px/1.65 system-ui,sans-serif;background:#f5f7fb;color:#192c48;max-width:1280px;margin:32px auto;padding:0 24px}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;padding:12px;border-bottom:1px solid #d5dce7}details{background:white;border:1px solid #d5dce7;border-radius:8px;padding:16px;margin:16px 0}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6fa;padding:16px;font-size:13px}select{font:inherit}label{display:inline-block;margin:16px 12px 0 0}</style>
<h1>OSK v9 · 100개 변경 / Sol·Terra</h1><p>기존 33개 + 추가 67개. 커밋별 독립 입력, medium, 모델별 1회.</p><p><b>실행 후 의미 판독은 요청에 따라 보류했습니다. 기계 검사 완료는 정답률이 아닙니다.</b></p>
<p>기존 삭제·혼합 커밋 규칙은 유지하고 삭제 보존 위치 검사, 실행 중 오류 피드백, 날짜 미상·관측일, 표기 정돈 보존을 추가했습니다. 운영 문서에는 적용하지 않았습니다.</p>'''+f'<p>총 {len(rows)}/200회 실행 기록 수집.</p>'+table+notes+filters+''.join(cards)+'''
<script>const modelSelect=document.getElementById('model'), caseSelect=document.getElementById('fid');function filter(){document.querySelectorAll('.case').forEach(e=>e.hidden=(modelSelect.value&&e.dataset.model!==modelSelect.value)||(caseSelect.value&&e.dataset.fid!==caseSelect.value));}modelSelect.onchange=filter;caseSelect.onchange=filter;</script></html>'''
    page = page.replace('OSK v9', 'OSK '+version).replace('기존 33개 + 추가 67개', esc(cohort))
    page = page.replace('요청에 따라 보류했습니다', '이 기계 집계에는 포함하지 않았습니다')
    if version == 'v10':
        page = page.replace('기존 삭제·혼합 커밋 규칙은 유지하고 삭제 보존 위치 검사, 실행 중 오류 피드백, 날짜 미상·관측일, 표기 정돈 보존을 추가했습니다.',
                            '네 가지 논지 영향 판정과 엔진의 출처 날짜 검증을 적용했습니다. 기존 v9와 표본·모델이 달라 직접적인 개선율 비교는 아닙니다.')
    (root/'report.html').write_text(page,encoding='utf-8')
    print(json.dumps(dict(completed=len(rows),summary=summary),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
