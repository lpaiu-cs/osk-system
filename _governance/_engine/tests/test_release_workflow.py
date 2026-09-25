"""Exercise the publication shell with a local gh stand-in; never contact GitHub."""
from pathlib import Path
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / '.github/workflows/release.yml'
BASH = (Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe'
        if os.name == 'nt' else shutil.which('bash'))
SHA = 'a' * 40
STUB = r'''
gh() {
  printf '%s\n' "$*" >> "$CALLS"
  case "$*" in
    "release view "*) return 1 ;;
    *"contents/release.json?ref="*) printf '%s' "$ATTESTATION" ;;
    *"git/matching-refs/"*)
      if [ "$REF_FAILURE" = 1 ]; then return 1; fi
      printf '%s' "$EXISTING" ;;
    *"releases/generate-notes"*) printf 'Release notes\n' ;;
    "release create "*) return "$RELEASE_FAILURE" ;;
    *) printf 'Unexpected gh call\n' >&2; return 1 ;;
  esac
}
'''


@unittest.skipUnless(WORKFLOW.exists() and BASH and Path(BASH).exists(), 'canonical workflow and bash required')
class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.load(WORKFLOW.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        self.steps = self.workflow['jobs']['mirror']['steps']
        self.step = next(step for step in self.steps if 'run' in step)

    def run_publication(self, *, tag='v4.0.0', version='v4.0.0', existing='', ref_failure='0',
                        release_failure='0'):
        with tempfile.TemporaryDirectory(prefix='osk-release-workflow-') as td:
            path = Path(td)
            script, calls = path / 'publish.sh', path / 'calls.txt'
            script.write_text(STUB + self.step['run'], encoding='utf-8', newline='\n')
            env = dict(os.environ, TAG=tag, COMMIT=SHA, REPO='fixture/repo',
                       CALLS=calls.as_posix(), EXISTING=existing, REF_FAILURE=ref_failure,
                       RELEASE_FAILURE=release_failure,
                       ATTESTATION=base64.b64encode(json.dumps({'version': version, 'files': {}}).encode()).decode())
            # The workflow uses python3; point the shell at this test's interpreter.
            wrapper = path / 'python3'
            wrapper.write_text('#!/bin/sh\nexec "' + Path(sys.executable).as_posix() + '" "$@"\n',
                               encoding='utf-8', newline='\n')
            wrapper.chmod(0o755)
            env['PATH'] = str(path) + os.pathsep + env['PATH']
            proc = subprocess.run([str(BASH), script.as_posix()], cwd=path, env=env,
                                  capture_output=True, text=True, encoding='utf-8', timeout=30)
            return proc, calls.read_text(encoding='utf-8').splitlines() if calls.exists() else []

    def test_full_checks_precede_publication_of_the_same_commit(self):
        self.assertEqual(set(self.workflow['on']), {'workflow_dispatch'})
        self.assertEqual(self.workflow['jobs']['mirror']['needs'], 'test')
        inputs = self.workflow['jobs']['test']['with']
        self.assertEqual(inputs['upgrade-matrix'], 'full')
        self.assertEqual(inputs['release-candidate'], '${{ inputs.version }}=${{ github.sha }}')
        self.assertEqual(self.step['env']['COMMIT'], '${{ github.sha }}')
        proc, calls = self.run_publication()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(any('release.json?ref=' + SHA in call for call in calls))
        self.assertIn('--target ' + SHA, calls[-1])
        self.assertTrue(calls[-1].startswith('release create v4.0.0 '))
        self.assertNotIn('--prerelease', calls[-1])

    def test_failed_preconditions_do_not_publish(self):
        for changes in ({'existing': 'b' * 40}, {'ref_failure': '1'},
                        {'version': 'v3.22.2'}, {'tag': 'v4.0.0-rc.1'}):
            with self.subTest(changes=changes):
                proc, calls = self.run_publication(**changes)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(any(call.startswith('release create ') for call in calls), calls)

    def test_retry_with_the_same_tag_commit_can_finish(self):
        proc, calls = self.run_publication(existing=SHA)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('--target ' + SHA, calls[-1])

    def test_token_can_publish_workflows_after_the_default_branch_moves(self):
        # GitHub requires Contents + Workflows write when the pinned commit's
        # workflows differ from the default branch. GITHUB_TOKEN cannot do this.
        token_step = next((step for step in self.steps if 'id' in step and
                           self.step['env']['GH_TOKEN'] ==
                           '${{ steps.' + step['id'] + '.outputs.token }}'), None)
        self.assertIsNotNone(token_step, 'publication must use the App token output')
        self.assertLess(self.steps.index(token_step), self.steps.index(self.step))
        self.assertTrue(token_step['uses'].startswith('actions/create-github-app-token@'))
        settings = token_step['with']
        self.assertEqual(settings['repositories'], '${{ github.repository }}')
        self.assertEqual({k: v for k, v in settings.items() if k.startswith('permission-')},
                         {'permission-contents': 'write', 'permission-workflows': 'write'})
        self.assertEqual(settings.get('skip-token-revoke', 'false'), 'false')

    def test_publication_api_failure_is_not_reported_as_success(self):
        proc, calls = self.run_publication(release_failure='1')
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sum(call.startswith('release create ') for call in calls), 1)


if __name__ == '__main__':
    if not WORKFLOW.exists() or not BASH or not Path(BASH).exists():
        print('SKIP: canonical release workflow and bash required')
        sys.exit(77)
    unittest.main()
