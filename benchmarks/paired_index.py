"""Same-fixture sequential A/B; reverse order on each repetition."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def run(a):
    assert (a.root / 'perf-fixture.json').is_file(), 'synthetic fixture required'
    processes = {}
    with a.output.open('w', encoding='utf-8') as output:
        def record(row):
            output.write(json.dumps(row) + '\n')
            output.flush()

        def call(variant, op, phase, rep=0, cold=False):
            p = processes[variant]
            p.stdin.write(json.dumps(dict(op=op, cold=cold)) + '\n')
            p.stdin.flush()
            line = p.stdout.readline()
            assert line, (variant, p.poll())
            result = json.loads(line)
            assert not result['racy'] and result['complete'], result
            record(dict(variant=variant, phase=phase, rep=rep, **result))
            return result

        try:
            for variant, engine in [('before', a.before), ('after', a.after)]:
                with a.output.with_suffix('.' + variant + '.stderr').open('w', encoding='utf-8') as err:
                    p = subprocess.Popen([sys.executable, str(Path(__file__).with_name('bench_index.py')),
                                          'worker', '--root', str(a.root), '--engine', str(engine),
                                          '--target', 'node-00020'], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=err, text=True, encoding='utf-8',
                                         creationflags=0x08000000 if sys.platform == 'win32' else 0)
                processes[variant] = p
                record(dict(variant=variant, phase='startup', **json.loads(p.stdout.readline())))
            for op in ('read_node', 'overview', 'search'):
                if a.only and a.only != op:
                    continue
                for i in range(a.repeat):
                    for variant in list(processes)[::1 if i % 2 else -1]:
                        call(variant, op, 'cold', i, cold=True)
                for i in range(a.warm):
                    for variant in list(processes)[::1 if i % 2 else -1]:
                        call(variant, op, 'warm', i)
            for kind, path in [('raw', 'Scope/Bench00/_raw/.records/sample.txt'),
                               ('ledger', 'Scope/Workbench/_ledger/bench.jsonl')]:
                if a.only and a.only != kind:
                    continue
                p = a.root / path
                original = p.read_bytes()
                try:
                    for i in range(a.repeat):
                        margins = [call(v, 'search', 'prime')['margin_ms'] for v in processes]
                        p.write_bytes(original + b'\n' * (i + 1))
                        time.sleep(max(margins) / 1000 + .01)
                        for variant in list(processes)[::1 if i % 2 else -1]:
                            call(variant, 'search', 'after-' + kind, i)
                finally:
                    p.write_bytes(original)
                    time.sleep(.03)
            record(dict(phase='metadata', fixture=str(a.root), python=sys.version,
                        before=str(a.before), after=str(a.after), repeat=a.repeat, warm=a.warm))
        finally:
            for p in processes.values():
                p.stdin.close()
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'before', 'after', 'output'):
        ap.add_argument('--' + name, type=Path, required=True)
    ap.add_argument('--repeat', type=int, default=3)
    ap.add_argument('--warm', type=int, default=20)
    ap.add_argument('--only', choices=['read_node', 'overview', 'search', 'raw', 'ledger'])
    run(ap.parse_args())
