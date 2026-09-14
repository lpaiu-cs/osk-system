"""Standalone Git Bash/native boundary checks. No network, device, or vault writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bash', type=Path, help='Git Bash executable (not WSL bash)')
    args = parser.parse_args()
    if os.name != 'nt':
        print('SKIP: this check requires Windows native Python and Git Bash')
        return
    git = shutil.which('git')
    candidates = ([args.bash] if args.bash else []) + (
        [Path(git).resolve().parents[1] / 'bin/bash.exe'] if git else [])
    candidates.append(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe')
    bash = next((p for p in candidates if p.is_file()), None)
    rtk = shutil.which('rtk')
    if not bash or not rtk:
        print('SKIP: Git Bash and RTK must be installed; pass --bash for a custom location')
        return
    # Only this child environment is changed, never the user's global conversion settings.
    env = dict(os.environ)
    for key in ('MSYS_NO_PATHCONV', 'MSYS2_ARG_CONV_EXCL'):
        env.pop(key, None)

    def run(command):
        return subprocess.run([str(bash), '--noprofile', '--norc', '-c', command],
                              env=env, capture_output=True, encoding='utf-8',
                              errors='replace', timeout=30, creationflags=0x08000000)

    checks = {}
    with tempfile.TemporaryDirectory(prefix='osk-shell-boundary-') as directory:
        root = Path(directory).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        probe = root / 'native_probe.py'
        probe.write_text(
            "import json,sys; from pathlib import Path\n"
            "mode=sys.argv[1]\n"
            "if mode=='argv': print(json.dumps(sys.argv[2:]))\n"
            "if mode=='failure':\n"
            "    print('ERROR_native_write_failed',file=sys.stderr,flush=True)\n"
            "    print('routine tail',file=sys.stderr,flush=True); sys.exit(7)\n"
            "if mode=='write': Path(sys.argv[2]).write_bytes(b'expected body\\n')\n",
            encoding='utf-8')

        def command(mode, *values):
            return shlex.join([Path(rtk).as_posix(), 'proxy', Path(sys.executable).as_posix(),
                               probe.as_posix(), mode, *values])

        paths = ['/repos/OWNER/REPO', 'repos/OWNER/REPO', '/sdcard/osk-probe.bin',
                 '/PID', 'C:/task/local.bin']
        original = run(command('argv', *paths))
        safe = run("MSYS2_ARG_CONV_EXCL='*' " + command('argv', *paths))
        assert original.returncode == safe.returncode == 0, (original, safe)
        mangled, preserved = json.loads(original.stdout), json.loads(safe.stdout)
        assert mangled[0] != paths[0] and mangled[2] != paths[2], mangled
        assert mangled[1] == paths[1] and mangled[4] == paths[4], mangled
        assert preserved == paths, preserved
        checks['native_argv'] = {'before': mangled, 'after': preserved}

        failure = command('failure')
        merged = run(failure + ' 2>&1')
        cut = run('set +o pipefail; ' + failure + ' 2>&1 | tail -1')
        pipefail = run('set -o pipefail; ' + failure + ' 2>&1 | tail -1')
        discarded = run(failure + ' >/dev/null 2>&1')
        assert merged.returncode == 7 and 'ERROR_native_write_failed' in merged.stdout, merged
        assert cut.returncode == 0 and 'ERROR_native_write_failed' not in cut.stdout, cut
        assert pipefail.returncode == 7 and 'ERROR_native_write_failed' not in pipefail.stdout, pipefail
        assert discarded.returncode == 7 and not (discarded.stdout or discarded.stderr), discarded
        checks['output'] = {'merged_exit': merged.returncode, 'tail_exit': cut.returncode,
                            'pipefail_tail_exit': pipefail.returncode,
                            'merged_keeps_error': True, 'tail_loses_error': True,
                            'discard_loses_error': True}

        # A local stand-in for a remote target: success must be checked at the target.
        target = root / 'target.txt'
        target.write_bytes(b'old body\n')
        expected = b'expected body\n'
        expected_hash = hashlib.sha256(expected).hexdigest()
        no_write = run(command('noop', target.as_posix()))
        assert no_write.returncode == 0
        assert target.read_bytes() != expected and hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash
        written = run(command('write', target.as_posix()))
        actual = target.read_bytes()
        assert written.returncode == 0 and actual == expected and hashlib.sha256(actual).hexdigest() == expected_hash
        checks['postimage'] = {'zero_exit_no_write_detected': True, 'body_and_hash_match_after_write': True,
                               'scope': 'local fixture, not a remote deployment test'}

        # The configured RTK hook must leave explicitly unfiltered commands intact.
        for text in ("rtk proxy gh api repos/OWNER/REPO",
                     "MSYS2_ARG_CONV_EXCL='*' rtk proxy adb shell sha256sum /sdcard/osk-probe.bin"):
            hooked = subprocess.run([rtk, 'hook', 'claude'], input=json.dumps({
                'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': text}}),
                capture_output=True, encoding='utf-8', timeout=15, creationflags=0x08000000)
            assert hooked.returncode == 0, hooked
            if hooked.stdout.strip():
                out = json.loads(hooked.stdout)
                rewritten = out.get('hookSpecificOutput', {}).get('updatedInput', {}).get('command', text)
                assert rewritten == text, (text, rewritten)
        checks['rtk_hook_preserves_explicit_proxy'] = True
    print(json.dumps({'result': 'PASS', 'checks': checks}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
