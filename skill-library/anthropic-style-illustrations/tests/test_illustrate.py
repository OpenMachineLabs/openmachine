"""Offline behavioral checks against the preserved index and real PNG backend."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location('illustrate', ROOT / 'scripts/illustrate.py')
m = importlib.util.module_from_spec(loader)
loader.loader.exec_module(m)


class IllustrateTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'runs').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'runs')
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.article, self.sha = m.read_article(ROOT / 'examples/article.md')
        self.spec = m.read_json(ROOT / 'examples/figure.json')

    def check_bad(self, spec, fragment):
        report = m.qa(self.article, self.sha, spec)
        self.assertFalse(report['ok'])
        self.assertIn(fragment, ' '.join(report['errors']))

    def test_real_index_preserved(self):
        records = m.load_index()
        self.assertEqual(len(records), 23)
        self.assertEqual(len({r['article_url'] for r in records}), 14)
        self.assertEqual(hashlib.sha256(m.INDEX.read_bytes()).hexdigest(),
                         'b5fde19b13780cd3919281702a477c5c66cc329ed106459a040ab26ebd4b3288')

    def test_index_requires_official_https_hosts(self):
        original = m.read_json(m.INDEX)
        for url in ('http://www-cdn.anthropic.com/fake.png', 'https://example.org/fake.png'):
            changed = copy.deepcopy(original)
            changed['records'][0]['image_url'] = url
            target = self.out / 'invalid-index.json'
            target.write_text(json.dumps(changed), encoding='utf-8')
            with patch.object(m, 'INDEX', target):
                with self.assertRaisesRegex(ValueError, 'invalid source links'):
                    m.load_index()

    def test_search_contrast_and_family(self):
        self.assertNotIn('E8', [r['case_id'] for r in m.search('')])
        self.assertEqual([r['case_id'] for r in m.search('E8', include_contrast=True)], ['E8'])
        self.assertTrue(all(r['family'] == 'flow' for r in m.search('', 'flow')))
        self.assertIn('A2', [r['case_id'] for r in m.search('negative branch')])

    def test_full_plan_fences_and_budget(self):
        article = '# First\n' + 'Substantial original prose. ' * 8
        article += '\n```md\n# Fake\n```\n## Second\n' + 'More substantial prose. ' * 8
        article += '\n~~~\n## Also fake\n~~~~\nLast section\n============\n'
        article += 'Final substantial prose. ' * 8
        plan = m.plan(article, 'hash', 1)
        self.assertEqual([s['title'] for s in plan['sections']], ['First', 'Second', 'Last section'])
        self.assertEqual([s['decision'] for s in plan['sections']], ['image', 'no-image', 'no-image'])
        self.assertIn('Final substantial', plan['sections'][-1]['body'])
        self.assertIn('no semantic', plan['approval'])

    def test_complete_article_sha(self):
        self.assertTrue(m.qa(self.article, self.sha, self.spec)['ok'])
        changed = self.article + '\nAn additional sentence.\n'
        self.assertFalse(m.qa(changed, hashlib.sha256(changed.encode()).hexdigest(), self.spec)['ok'])
        self.check_bad(dict(self.spec, article_sha256='fake'), 'SHA mismatch')

    def test_happy_render_and_manual_edit(self):
        self.spec['title'] = 'A manually edited title'
        self.spec['nodes'][0]['label'] = 'Record the research question'
        report = m.render(self.article, self.sha, self.spec, self.out)
        self.assertTrue(report['ok'])
        self.assertFalse(report['semantic_approval'])
        self.assertFalse(report['visual_approval'])
        self.assertEqual(len(list(self.out.iterdir())), 5)
        ET.parse(self.out / 'figure.svg')
        self.assertEqual((self.out / 'figure.svg').read_text().count('<polygon'), 3)
        self.assertTrue((self.out / 'figure.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertIn('A manually edited title', (self.out / 'preview.html').read_text())
        self.assertEqual(m.read_json(self.out / 'figure.json'), self.spec)

    def test_missing_png_backend_is_explicit(self):
        with patch.dict(sys.modules, {'fitz': None}):
            with self.assertRaisesRegex(ValueError, r'PNG export requires PyMuPDF'):
                m.render(self.article, self.sha, self.spec, self.out)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_original_draft_quotes_and_no_inferred_edges(self):
        spec = m.draft(self.article, self.sha)
        self.assertEqual(spec['edges'], [])
        self.assertTrue(all(n['label'] == n['quote'] and n['quote'] in self.article for n in spec['nodes']))
        m.render(self.article, self.sha, spec, self.out)
        self.assertNotIn('<image', (self.out / 'figure.svg').read_text())

    def test_unicode_escape_and_citation_links(self):
        quote = 'Café & tea <review> "yes" — [notes](https://example.org/a?x=1&y=2).'
        spec = m.draft(quote, 'hash')
        spec['title'] = '<script>alert("x")</script> & café'
        self.assertTrue(m.qa(quote, 'hash', spec)['ok'])
        m.render(quote, 'hash', spec, self.out)
        root = ET.parse(self.out / 'figure.svg').getroot()
        self.assertTrue(root.findall('.//{http://www.w3.org/2000/svg}a'))
        markup = (self.out / 'preview.html').read_text()
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;review&gt;', markup)
        self.assertIn('href="https://example.org/a?x=1&amp;y=2"', markup)

    def test_missing_and_fake_quote(self):
        for value in (None, '', 'A fabricated assertion.'):
            spec = copy.deepcopy(self.spec)
            spec['nodes'][0]['quote'] = value
            self.check_bad(spec, 'quote')

    def test_invalid_edges_and_case_references(self):
        for field, value, fragment in [('to', 'unknown', 'endpoints'), ('id', 'n1', 'edge ID')]:
            spec = copy.deepcopy(self.spec)
            spec['edges'][0][field] = value
            self.check_bad(spec, fragment)
        for ref in ({'case_id': 'fake', 'article_url': 'https://example.org'},
                    {'case_id': 'A2', 'article_url': 'https://example.org'}):
            self.check_bad(dict(self.spec, case_refs=[ref]), 'case reference')
        e8 = next(r for r in m.load_index() if r['case_id'] == 'E8')
        self.check_bad(dict(self.spec, case_refs=[e8]), 'Negative contrast')

    def chart(self, family='bar'):
        article = 'First took 12 ms. Second took 24 ms.'
        spec = m.draft(article, 'hash')
        spec.update(family=family, baseline=0, unit='ms', nodes=[
            {'id': 'a', 'label': 'First', 'value': 12, 'quote': 'First took 12 ms.'},
            {'id': 'b', 'label': 'Second', 'value': 24, 'quote': 'Second took 24 ms.'}])
        return article, spec

    def test_chart_numbers_units_baseline(self):
        article, original = self.chart()
        for value in (9, float('nan'), float('inf'), -1, True):
            spec = copy.deepcopy(original)
            spec['nodes'][0]['value'] = value
            self.assertFalse(m.qa(article, 'hash', spec)['ok'])
        for patch in ({'unit': ''}, {'unit': 's'}, {'baseline': 1}, {'unit': None}):
            self.assertFalse(m.qa(article, 'hash', dict(original, **patch))['ok'])
        spec = copy.deepcopy(original)
        spec['nodes'][0]['quote'] = 'First took 12'
        self.assertFalse(m.qa(article, 'hash', spec)['ok'])
        spec['nodes'][0].update(value=2, quote='First took 12 ms.')
        self.assertFalse(m.qa(article, 'hash', spec)['ok'])

    def test_all_remaining_families(self):
        for family in ('bar', 'line'):
            article, spec = self.chart(family)
            m.render(article, 'hash', spec, self.out / family)
            ET.parse(self.out / family / 'figure.svg')
        for family in ('matrix', 'cover'):
            spec = m.draft(self.article, self.sha)
            spec['family'] = family
            if family == 'matrix':
                spec['columns'] = ['Evidence']
                for node in spec['nodes']:
                    node['cells'] = [{'text': 'Exact article excerpt', 'quote': node['quote']}]
            m.render(self.article, self.sha, spec, self.out / family)
            ET.parse(self.out / family / 'figure.svg')

    def test_output_traversal_and_symlinks(self):
        for path in (ROOT / '..' / 'escape', '/tmp/illustration-escape'):
            with self.assertRaisesRegex(ValueError, 'inside Skill'):
                m.render(self.article, self.sha, self.spec, path)
        link = self.out / 'escape'
        link.symlink_to('/tmp', target_is_directory=True)
        with self.assertRaises(ValueError):
            m.write_text(link / 'not-written.txt', 'x')
        child = self.out / 'figure.svg'
        child.symlink_to(ROOT / 'SKILL.md')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            m.render(self.article, self.sha, self.spec, self.out)
        self.assertFalse((self.out / 'figure.png').exists())

    def test_cli_qa_failure_and_nonfinite_json(self):
        path = self.out / 'invalid.json'
        path.write_text(json.dumps(dict(self.spec, article_sha256='fake')))
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/illustrate.py'),
                                 'qa', str(ROOT / 'examples/article.md'), str(path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['ok'])
        path.write_text('{"value": NaN}')
        with self.assertRaisesRegex(ValueError, 'Nonfinite'):
            m.read_json(path)


if __name__ == '__main__':
    unittest.main()
