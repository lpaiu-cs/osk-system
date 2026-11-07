"""Keep the original 33 fixtures; select 67 more historical edits without reading model outcomes."""
import argparse
from collections import Counter, defaultdict
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import abstraction_paths as ap


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault', type=Path, required=True)
    p.add_argument('--original', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--fresh', action='store_true', help='Select 100 new edits, excluding all original commits and exact body diffs')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    def git(*a):
        return subprocess.run(['git','-C',str(args.vault),'-c','core.quotepath=off',*a],
                              check=True,capture_output=True,text=True,encoding='utf-8').stdout
    head = git('rev-parse','HEAD').strip()
    spaces = ['= Scope','= Domain','00_Scope','00_Domain']
    def is_node(path):
        parts = path.split('/')
        return (len(parts)>1 and parts[0] in spaces and path.endswith('.md')
                and not any(x.startswith('_') for x in parts[1:])
                and 'Workbench' not in parts and Path(path).stem != parts[-2])
    blobs = ap.Blobs(str(args.vault))
    paths = [s for s in git('ls-tree','-r','--name-only',head,'--',*spaces).splitlines() if is_node(s)]
    ids, leaning = {}, defaultdict(set)
    for path in paths:
        fm = ap.split(blobs.get(head,path) or '')[0]
        ids[Path(path).stem] = ap.node_id(fm)
        for ref in ap.dfrom(fm):
            leaning[ref].add(path)
    picked, original = [], []
    for f in sorted(args.original.glob('F*.json')):
        item = json.loads(f.read_text(encoding='utf-8'))
        original.append(item)
        if not args.fresh:
            picked.append(item)
            shutil.copyfile(f,args.output/f.name)
    assert len(original)==(100 if args.fresh else 33)
    assert not args.fresh or not list(args.output.glob('F*.json')), 'fresh output directory required'
    start_id = max(int(f['id'][1:]) for f in original) if args.fresh else 0
    seed = 'v10-fresh100-20261106' if args.fresh else 'v9-100-20261106'
    used = {(f['sha'],f['path']) for f in original}
    excluded_commits = {f['sha'] for f in original} if args.fresh else set()
    excluded_diffs = {f['diff'] for f in original} if args.fresh else set()
    per_node = Counter(f['path'] for f in picked)
    candidates, commit = [], None
    log = git('log','--since=2026-09-26','--format=@@%H %cI','--name-only',head,'--',*spaces)
    for line in log.splitlines():
        if line.startswith('@@'):
            commit = line[2:].split(' ',1)
        elif is_node(line) and commit and (commit[0],line) not in used and commit[0] not in excluded_commits:
            stem = Path(line).stem
            deps = leaning.get(stem,set()) | leaning.get(ids.get(stem),set())
            if deps:
                candidates.append((commit[0],commit[1],line,len(deps)))
    # Seeded hash order is stable; repeatedly prefer less represented target documents.
    candidates.sort(key=lambda x: hashlib.sha256((seed+x[0]+x[2]).encode()).hexdigest())
    rejected = Counter()
    while candidates and len(picked)<100:
        pos = min(range(len(candidates)),key=lambda i: per_node[candidates[i][2]])
        sha,at,path,n_deps = candidates.pop(pos)
        parents = git('rev-list','--parents','-n','1',sha).split()[1:]
        if len(parents)!=1:
            rejected['not_single_parent']+=1
            continue
        parent = parents[0]
        before,after = blobs.get(parent,path),blobs.get(sha,path)
        if not before or not after:
            rejected['new_or_removed']+=1
            continue
        _,bb,be = ap.split(before)
        fm,ab,ae = ap.split(after)
        diff = '\n'.join(difflib.unified_diff(bb.splitlines(),ab.splitlines(),'before','after',n=3,lineterm=''))
        if not 80<=len(diff)<=7000:
            rejected['diff_size']+=1
            continue
        if diff in excluded_diffs:
            rejected['duplicate_body_diff']+=1
            continue
        keys = [Path(path).stem, ap.node_id(fm)]
        query = ['grep','-l','-F']
        for key in keys:
            if key:
                query.extend(['-e',key])
        try:
            hits = git(*query,sha,'--',*spaces).splitlines()
        except subprocess.CalledProcessError:
            hits = []
        deps = []
        for hit in hits:
            other = hit.split(':',1)[1]
            if is_node(other) and other!=path and set(ap.dfrom(ap.split(blobs.get(sha,other) or '')[0])) & set(keys):
                deps.append(Path(other).stem)
        if not deps:
            rejected['no_historical_dependent']+=1
            continue
        item = dict(id=f'F{start_id+len(picked)+1:02d}',sha=sha,parent=parent,at=at,path=path,
                    stem=Path(path).stem,diff=diff,dependents=sorted(deps),n_deps=n_deps,
                    added_coords=sorted(set(ap.COORD.findall(ae))-set(ap.COORD.findall(be))),
                    stratum='expanded',domain=path.startswith(('= Domain','00_Domain')),pair=None)
        picked.append(item)
        per_node[path]+=1
        used.add((sha,path))
        if args.fresh:
            excluded_diffs.add(diff)
        (args.output/(item['id']+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf-8')
    blobs.p.stdin.close()
    blobs.p.wait()
    assert len(picked)==100, len(picked)
    assert len({(f['sha'],f['path']) for f in picked})==100
    report = dict(head=head,count=len(picked),original=0 if args.fresh else 33,expanded=100 if args.fresh else 67,unique_targets=len(per_node),
                  excluded_original=len(original) if args.fresh else 0, excluded_commits=len(excluded_commits),
                  seed=seed,since='2026-09-26',excluded_person=True,
                  selection='hash order, least represented target first; no outcome labels',
                  rejected=dict(rejected),fixtures={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in args.output.glob('F*.json')})
    (args.output/'selection.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='fixtures'},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
