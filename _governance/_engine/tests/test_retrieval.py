"""Read-only retrieval contracts plus a rejected partial-to-full write, in a mini-vault."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))


def search_checks():
    from osk import core, search

    class Index:
        def __init__(self):
            self.nodes, self.parsed = {}, {}
            for i, (title, body, kind) in enumerate([
                ('Long exact title', 'unrelated filler ' * 10000, 'scope'),
                ('Impostor', 'Long exact title ' * 30, 'scope'),
                ('LONG EXACT TITLE', 'Long exact title', 'scope'),
                ('!!!', 'punctuation only title', 'scope'),
                ('Hidden', 'Long exact title', 'workbench-transit'),
                ('Rules', 'Long exact title', 'governance'),
                ('Other', 'independent control', 'scope'),
            ]):
                path = core.ROOT / f'{i}.md'
                self.nodes[title] = (path, (kind, 'W1'))
                self.parsed[path] = SimpleNamespace(id=str(i), meta={}, body=body)

        def node(self, path):
            return self.parsed[path]

    s = search.Searcher(Index())
    for query, expected in [('Long exact title', 'Long exact title'),
                            ('LONG EXACT TITLE', 'LONG EXACT TITLE'), ('!!!', '!!!')]:
        assert s.work_search(query, 1)[0]['title'] == expected, query
    assert s.work_search('  long EXACT title  ', 1)[0]['title'] in {
        'Long exact title', 'LONG EXACT TITLE'}
    assert not s.work_search('Hidden') and not s.work_search('Rules')
    assert not s.work_search('absentword')
    # Non-title queries retain the original BM25 order and score, including zero IDF.
    for query in ('title filler', 'independent', 'punctuation'):
        q = search._tokens(query)
        scores = s.bm25.get_scores(q)
        expected = sorted([(float(score), item[0])
                           for score, item, tokens in zip(scores, s.paths, s.tokens)
                           if set(q) & tokens], key=lambda item: -item[0])
        actual = s.view_search(query, 50)
        assert [(h['score'], h['title']) for h in actual] == [
            (round(score, 3), title) for score, title in expected]


def read_checks():
    import mcp_server as m
    from osk import core, validate, write
    validate.make_mini_vault(core.ROOT)
    body = ('Introduction 한글😀\n# First\n' + '가😀 value\n' * 800 +
            '\n```markdown\n# Fake\n```\n~~~\n## Also fake\n~~~\n'
            '\n- ```python\n  # List comment\n  ```\n'
            '## Repeated\nFirst instance\n## Repeated\nSecond instance\n')
    assert write.create_node('Selective read', 'Read fixture', body,
                             'gpt-6-astra', space='= Scope/W1')['ok']
    path = core.ROOT / '= Scope/W1/Selective read.md'
    path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
    full = m.read_node('Selective read')
    outline = m.read_node(full['id'], view='outline')
    assert outline['partial'] and 'hash' not in outline and 'body' not in outline
    assert outline['body_chars'] == len(full['body'])
    assert [h['title'] for h in outline['headings']] == ['First', 'Repeated', 'Repeated']
    for heading in outline['headings']:
        got = m.read_node(full['name'], view=heading['view'])
        assert got['body'].lstrip().startswith('#')
        assert got['view_hash'] == outline['view_hash']
    chunks, view = [], f"0:{outline['body_chars']}"
    while view:
        part = m.read_node(full['name'], view=view)
        assert part['partial'] and 'hash' not in part and len(part['body']) <= 4000
        assert part['body'] == full['body'][part['start']:part['end']]
        chunks.append(part['body'])
        view = part['next_view']
    assert ''.join(chunks) == full['body']
    for view in ('-1:4', '3:2', '0:0', '0:9999999999999999999999',
                 f"{len(full['body']) + 1}:{len(full['body']) + 2}", 'garbage'):
        assert 'error' in m.read_node(full['name'], view=view), view
    refused = m.update_node(full['name'], body='Truncated replacement',
                            expect_hash=outline['view_hash'])
    assert not refused['ok'] and path.read_bytes().decode('utf-8').find('Truncated replacement') == -1
    assert m.read_node(full['name']) == full
    assert m.update_node(full['name'], old_text='First instance', new_text='Revised instance')['ok']
    assert m.read_node(full['name'], view='outline')['view_hash'] != outline['view_hash']
    # A heading-free body is still pageable; huge outlines have a visible ceiling.
    assert write.create_node('No headings', 'Plain', 'plain ' * 900,
                             'gpt-6-astra', space='= Scope/W1')['ok']
    assert not m.read_node('No headings', view='outline')['headings']
    assert m.read_node('No headings', view='0:5000')['next_view']
    assert write.create_node('Many headings', 'Outline', '\n'.join(f'# H{i}' for i in range(100)),
                             'gpt-6-astra', space='= Scope/W1')['ok']
    many = m.read_node('Many headings', view='outline')
    assert len(many['headings']) == 40 and many['outline_truncated']
    # List fences must hide code headings without swallowing the following section.
    for block in (
        '- ```python\n  # comment\n  ```\n',
        '10. ~~~~python\n    # comment\n    ~~~\n    ## still code\n    ~~~~~\n',
        '- item\n\n  ```python\n  # comment\n  ```\n',
        '- outer\n  - inner\n\n    ```python\n    # comment\n    ```\n',
        '- - ```python\n    # comment\n    ```\n',
        '-\t```python\n\t# comment\n\t```\n',
        '- ```python\n  # comment\n',  # Leaving the list also closes an unclosed fence.
        '- ```python\n  # comment\n- next item\n',
    ):
        sample = '# Before\n\n' + block + '\n## After\n중요한 결론😀\n'
        selected = m._node_view(sample, 'outline')
        assert [h['title'] for h in selected['headings']] == ['Before', 'After'], (block, selected)
        assert not selected['outline_truncated']
        after = selected['headings'][1]
        assert after['start'] == sample.index('## After')
        assert m._node_view(sample, after['view'])['body'] == '## After\n중요한 결론😀\n'
    assert m._node_view('```in`valid\n# Visible\n', 'outline')['headings'][0]['title'] == 'Visible'
    assert not validate.surface_lint(), validate.surface_lint()


async def transport_checks():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable,
                                   args=['-B', str(ENGINE / 'mcp_server.py')], env=dict(os.environ))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert all(t.inputSchema.get('additionalProperties') is False for t in tools)
            schema = next(t.inputSchema for t in tools if t.name == 'read_node')
            assert 'view' in schema['properties'] and schema['required'] == ['name']
            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.isError, result
                return json.loads(result.content[0].text)
            full = await call('read_node', {'name': 'Selective read'})
            for wrong in (
                {'old_string': 'Revised instance', 'new_string': 'Lost edit'},
                {'summmary': 'Misspelled summary'},
            ):
                refused = await session.call_tool('update_node', {
                    'name': full['name'], 'summary': 'Must not be partially saved', **wrong})
                assert refused.isError and all(
                    key in str(refused.content) for key in wrong), refused
                assert await call('read_node', {'name': full['name']}) == full
            # The shared registration boundary also rejects mistaken read options.
            refused = await session.call_tool('read_node', {'name': full['name'], 'veiw': 'outline'})
            assert refused.isError and 'veiw' in str(refused.content), refused
            valid = await call('update_node', {'name': full['name'],
                'old_text': 'Revised instance', 'new_text': 'Stored edit'})
            assert valid['ok']
            stored = await call('read_node', {'name': full['name']})
            assert 'Stored edit' in stored['body'] and stored['body'] != full['body']
            assert (await call('update_node', {'name': full['name'],
                'old_text': 'Stored edit', 'new_text': 'Revised instance'}))['ok']
            full = await call('read_node', {'name': full['name']})
            outline = await call('read_node', {'name': full['name'], 'view': 'outline'})
            assert [h['title'] for h in outline['headings']] == ['First', 'Repeated', 'Repeated']
            part = await call('read_node', {'name': 'Selective read', 'view': '0:20'})
            assert part['body'] == full['body'][:20] and 'hash' not in part
            refused = await call('update_node', {'name': full['name'], 'body': part['body'],
                                                'expect_hash': part['view_hash']})
            assert not refused['ok']
            assert await call('read_node', {'name': full['name']}) == full
            assert (await call('update_node', {'name': full['name'], 'body': full['body'],
                                               'expect_hash': full['hash']}))['ok']


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='osk-retrieval-test-') as directory:
        os.environ.update(OSK_VAULT_ROOT=directory, PYTHONPATH=str(ENGINE),
                          PYTHONDONTWRITEBYTECODE='1')
        search_checks()
        read_checks()
        asyncio.run(asyncio.wait_for(transport_checks(), 60))
        print('PASS: exact title / unchanged BM25 / bounded reads / MCP CAS boundary')
