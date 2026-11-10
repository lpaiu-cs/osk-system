import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from abstraction_trace import audit_run, collect_trace, save


class TraceTests(unittest.TestCase):
    def test_runner_does_not_reuse_another_prompt_version(self):
        from types import SimpleNamespace
        from abstraction_v9 import one
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / 'F01.json'
            save(fixture, {'id':'F01'})
            out = root / 'runs/F01_model_1'
            out.mkdir(parents=True)
            save(out / 'done.json', {})
            save(out / 'planned.json', {'prompt_version':'v9'})
            args = SimpleNamespace(output=root, prompt_version='v10')
            with self.assertRaisesRegex(ValueError, 'another prompt version'):
                one(fixture, 'model', 1, args)
            args.prompt_version = 'v9'
            self.assertIsNone(one(fixture, 'model', 1, args))

    def test_missing_is_unknown_and_rollout_retains_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # JSONL is one compact record per line, not a pretty-printed JSON document.
            (root / 'events.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': 'test-id'}))
            start = datetime.now(timezone.utc).isoformat()
            self.assertIsNone(collect_trace(root, root, start)['compaction_count'])
            folder = root / 'sessions' / datetime.now(timezone.utc).strftime('%Y/%m/%d')
            folder.mkdir(parents=True)
            events = [{'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'model_context_window': 123}}},
                      {'type': 'event_msg', 'payload': {'type': 'context_compacted'}},
                      {'type': 'compacted', 'payload': {'message': 'test'}}]
            path = folder / 'rollout-test-id.jsonl'
            path.write_text('\n'.join(json.dumps(e) for e in events))
            got = collect_trace(root, root, start)
            self.assertEqual(got['compaction_count'], 1)
            self.assertEqual(got['token_usage'][0]['info']['model_context_window'], 123)
            self.assertEqual((root / 'rollout.jsonl').read_bytes(), path.read_bytes())
            path.write_text(json.dumps(events[1]))
            self.assertEqual(collect_trace(root, root, start)['compaction_count'], 1)
            path.write_text(json.dumps(events[0]))
            self.assertEqual(collect_trace(root, root, start)['compaction_count'], 0)

    def test_repeats_distinguish_view_write_and_changed_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'prompt.txt').write_text('test')
            save(root / 'done.json', {'model': 'test'})
            calls = [('read_node', {'name': 'a'}, 'old'), ('read_node', {'name': 'a'}, 'old'),
                     ('read_node', {'name': 'a', 'view': '0:10'}, 'old'),
                     ('update_node', {'name': 'b'}, 'ok'), ('read_node', {'name': 'a'}, 'old'),
                     ('read_node', {'name': 'a'}, 'new')]
            events = [{'type': 'item.completed', 'item': {'id': str(i), 'type': 'mcp_tool_call',
                       'tool': tool, 'arguments': args, 'result': result}}
                      for i, (tool, args, result) in enumerate(calls)]
            (root / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
            repeats = audit_run(root)['identical_repeats']
            self.assertEqual(len(repeats), 2)
            self.assertTrue(repeats[0]['consecutive'])
            self.assertTrue(repeats[0]['no_intervening_write'])
            self.assertFalse(repeats[1]['no_intervening_write'])


if __name__ == '__main__':
    unittest.main()
