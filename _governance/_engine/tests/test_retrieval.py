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
                self.parsed[path] = SimpleNamespace(id=str(i), meta={}, body=body,
                                                    edges=lambda _predicate: [], wikilinks=lambda _end=None: [])

        def node(self, path):
            return self.parsed[path]

    s = search.Searcher(core.VAULT, Index())
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


def ranking_checks():
    """Exposure ranking (Bylaws §8 3, Mechanism §8 8): recency, centrality and locality multiply
    relevance; a hub gets neither recency nor centrality; locality pulls, never filters."""
    from datetime import datetime, timedelta
    from osk import core, search
    now = datetime.now(core.KST)

    class Index:
        def __init__(self, specs):
            self.nodes, self.parsed = {}, {}
            for title, space, days, links in specs:
                path = core.ROOT / space[1] / f'{title}.md'
                self.nodes[title] = (path, space)
                self.parsed[path] = SimpleNamespace(
                    id=title, body='shared ranking words', edges=lambda _predicate: [],
                    meta={'updated': (now - timedelta(days=days)).strftime(core.TS_FMT)},
                    wikilinks=lambda _end=None, links=links: links)

        def node(self, path):
            return self.parsed[path]

        def resolve(self, name):
            return ('node', self.nodes[name][1]) if name in self.nodes else ('dangling',)

    w1, w2 = ('scope', 'W1'), ('scope', 'W2')
    s = search.Searcher(core.VAULT, Index([
        ('Fresh', w1, 0, []), ('Stale', w1, 365, []), ('Core', w1, 365, []),
        *[(f'Link {i}', w1, 365, ['Core', 'W1']) for i in range(3)],
        ('W1', w1, 0, []),                      # the W1 hub: fresh and linked, boosted by neither
        ('Other', w2, 365, []), ('Domain', ('domain', 'D'), 365, [])]))
    base = s.bm25.get_scores(search._tokens('ranking'))[0]
    score = lambda local=None: {h['title']: h['score'] for h in s.view_search('ranking', 50, local)}
    plain = score()
    assert abs(plain['Fresh'] - base * (1 + search.W_RECENT)) < 0.002, plain
    assert abs(plain['Core'] - base * (1 + search.W_CENTRAL)) < 0.002, plain
    assert plain['W1'] == round(base, 3) and plain['Stale'] < plain['Fresh'], plain
    assert [h['title'] for h in s.view_search('ranking', 2)] in (['Fresh', 'Core'], ['Core', 'Fresh'])
    # The same searcher ninety days on, files untouched: recency decays at search time (#234 review).
    from unittest import mock
    with mock.patch.object(search.time, 'time', return_value=now.timestamp() + 90 * 86400):
        later = score()
    assert abs(later['Fresh'] - base * (1 + search.W_RECENT / 8)) < 0.002, later
    assert abs(later['Core'] - plain['Core']) < 0.002 and later['Core'] > later['Fresh'], later
    near = score({'scopes': ['W1', 'W2'], 'primary': 'W2'})
    assert abs(near['Other'] - base * (1 + search.W_PRIMARY)) < 0.002, near
    assert abs(near['Stale'] - base * (1 + search.W_SCOPE)) < 0.002 and near['Domain'] == plain['Domain'], near
    assert len(near) == len(plain), 'locality filtered a result'


def centrality_checks():
    """C(v) = (E(v), D(v)) (Constitution 11.2, Mechanism §8 6): a reference raises the referrer's E
    and the referred's D by the same weight; observation lines add to E only — 지지 +1, 반례 −1,
    조건 0. R counts presence: a repeated reference counts once, and self-references, a hub's
    references and Links inside the observation section are not in R, so Σ D = Σ (E − observation
    term). Workbench and governance count on neither side."""
    import re
    from osk import core, graph

    class Index:
        def __init__(self, specs):
            self.nodes, self.parsed = {}, {}
            for title, kind, derived, body in specs:
                path = core.ROOT / 'W1' / f'{title}.md'
                self.nodes[title] = (path, (kind, 'W1'))
                self.parsed[path] = SimpleNamespace(
                    id=title, meta={}, body=body,
                    edges=lambda pred, d=derived: d if pred == 'derived-from' else [],
                    wikilinks=lambda end=None, b=body: re.findall(r'\[\[([^\]|#]+)', b[:end]))

        def node(self, path):
            return self.parsed[path]

        def resolve(self, name):
            return ('node', self.nodes[name][1]) if name in self.nodes else ('dangling',)

    obs = '\n'.join(['claim on [[B]], again [[B]], itself [[A]] and [[Gone]]', '', '# 관측', '',
                     '- 2026-11-09 · `a1b2c3d4#1` · 지지 — explained',
                     '- 2026-11-09 · `a1b2c3d4#2` · 지지 — explained too, read with [[C]]',
                     '- 2026-11-09 · `a1b2c3d4#3` · 조건 — under a condition',
                     '- 2026-11-09 · `a1b2c3d4#4` · 반례 — not explained, against [[B]]',
                     'not a line'])
    nodes = Index([
        ('B', 'scope', [], 'base'),
        ('C', 'scope', ['B', 'B'], 'c'),
        ('A', 'scope', ['B', 'A'], obs),
        ('W1', 'scope', ['C'], '[[A]] [[B]]'),
        ('Note', 'workbench-transit', ['B'], 'transit'),
    ])
    explain, depend = graph.centrality(core.VAULT, nodes)
    assert explain['A'] == 3 + 1 + (2 - 1), explain     # derived-from 3, Link 1, 지지 2 − 반례 1
    assert explain['C'] == 3, explain                   # the repeated derived-from counts once
    assert depend['B'] == 3 + 1 + 3 and depend['A'] == 0 and depend['C'] == 0, depend
    assert explain['W1'] == 0 and explain['B'] == 0 and explain['Note'] == 0, explain
    # Σ D = Σ (E − w_O·(|O⁺| − |O⁻|)) — only A has observation lines, net 2 − 1
    assert sum(depend.values()) == sum(explain.values()) - (2 - 1), (explain, depend)
    legacy = obs.replace('# 관측', '## 근거')
    assert graph.centrality(core.VAULT, Index([('A', 'scope', [], legacy)]))[0]['A'] == 1


def read_checks():
    import mcp_server as m
    from osk import core, validate, write
    validate.make_mini_vault(core.VAULT, core.ROOT)
    body = ('Introduction 한글😀\n# First\n' + '가😀 value\n' * 800 +
            '\n```markdown\n# Fake\n```\n~~~\n## Also fake\n~~~\n'
            '\n- ```python\n  # List comment\n  ```\n'
            '## Repeated\nFirst instance\n## Repeated\nSecond instance\n')
    assert write.create_node(core.VAULT, 'Selective read', 'Read fixture', body,
                             'gpt-6-astra', space='00_Scope/W1')['ok']
    path = core.ROOT / '00_Scope/W1/Selective read.md'
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
    # A quote returns its paragraph plus one each side; that window is the edit's anchor (#273).
    assert write.create_node(core.VAULT, 'Window read', 'Anchor window',
                             '# 결론\n\n- 첫째.\n- 둘째에 59덱이 있다.\n  - 하위.\n- 셋째.\n\n산문 첫 줄\n이어짐.',
                             'gpt-6-astra', space='00_Scope/W1')['ok']
    got = m.read_node('Window read', view='59덱')
    assert got['partial'] and got['body'] == '- 첫째.\n- 둘째에 59덱이 있다.\n  - 하위.', got
    assert m.read_node('Window read', view='이어짐')['body'] == '- 셋째.\n\n산문 첫 줄\n이어짐.'
    assert m.update_node('Window read', old_text=got['body'], new_text='- 첫째.\n- 둘째가 있다.\n  - 하위.')['ok']
    many = m.read_node('Window read', view='째')
    refused = m.update_node('Window read', old_text='째', new_text='x')
    assert 'error' in many and not refused['ok'] and refused['views'] == many['views'] and len(many['views']) == 3
    assert 'error' in m.read_node('Window read', view='없는 문장')
    assert m.update_node('Window read', old_text='- 셋째.', new_text='- 셋째 #9x.')['ok']
    tag = m.read_node('Window read', view='셋째 #9x')  # 태그 방어(#292) 뒤의 저장본에도 맞는다
    assert '#9 x' in tag.get('body', ''), tag
    # `search(session=…)` reads the session's bindings: its primary scope's nodes rank up (#170).
    # Scores in this two-node vault are negative (common terms' idf): the boost still pulls up.
    write.set_primary(core.VAULT, 'retrieval-key', 'W1', 'fixture')
    assert write.bindings(core.VAULT, 'retrieval-key')['primary'] == 'W1'
    plain = {h['title']: h['score'] for h in m.search('Selective read', 5)}
    near = {h['title']: h['score'] for h in m.search('Selective read', 5, session='retrieval-key')}
    assert near['Selective read'] > plain['Selective read'] and near.keys() == plain.keys(), (plain, near)
    # A heading-free body is still pageable; huge outlines have a visible ceiling.
    assert write.create_node(core.VAULT, 'No headings', 'Plain', 'plain ' * 900,
                             'gpt-6-astra', space='00_Scope/W1')['ok']
    assert not m.read_node('No headings', view='outline')['headings']
    assert m.read_node('No headings', view='0:5000')['next_view']
    assert write.create_node(core.VAULT, 'Many headings', 'Outline', '\n'.join(f'# H{i}' for i in range(100)),
                             'gpt-6-astra', space='00_Scope/W1')['ok']
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
    assert not validate.surface_lint(core.VAULT), validate.surface_lint(core.VAULT)


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
        ranking_checks()
        centrality_checks()
        read_checks()
        asyncio.run(asyncio.wait_for(transport_checks(), 60))
        print('PASS: exact title / unchanged BM25 / bounded reads / MCP CAS boundary')
