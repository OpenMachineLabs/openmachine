"""Offline behavioral checks against the preserved index and real PNG backend."""
import base64
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

ROOT = Path(__file__).resolve().parents[3] / 'skill-library/anthropic-style-illustrations'
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

    def render(self, *args):
        if importlib.util.find_spec('fitz') is not None:
            return m.render(*args)
        # Hosted Open Machine wheel tests do not install this standalone Skill's
        # optional PNG backend; verify real SVG XML and the SVG/HTML/QA pipeline.
        def validate_svg(svg):
            ET.fromstring(svg)
            return base64.b64decode(
                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC'
                'AAAAC0lEQVR42mP8/x8AAwMCAO+/GXcAAAAASUVORK5CYII=')
        with patch.object(m, 'png_bytes', side_effect=validate_svg):
            return m.render(*args)

    def check_bad(self, spec, fragment):
        report = m.qa(self.article, self.sha, spec)
        self.assertFalse(report['ok'])
        self.assertIn(fragment, ' '.join(report['errors']))

    def test_real_index_preserved(self):
        records = m.load_index()
        self.assertGreaterEqual(len(records), 20)
        self.assertGreaterEqual(len({r['article_url'] for r in records}), 12)
        self.assertEqual(len({r['case_id'] for r in records}), len(records))
        self.assertTrue(all(r['provenance'].get('reviewer') and
                            r['provenance'].get('method') and r['provenance'].get('date')
                            for r in records))

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

    def test_more_sources_do_not_require_code_changes(self):
        index = m.read_json(m.INDEX)
        existing = index['records'][0]
        extra = copy.deepcopy(existing)
        extra['case_id'] = 'NEWCASE'
        extra['article_url'] = 'https://www.anthropic.com/engineering/future-publication'
        index['records'].append(extra)
        path = self.out / 'extended-index.json'
        path.write_text(json.dumps(index), encoding='utf-8')
        with patch.object(m, 'INDEX', path):
            self.assertEqual(len(m.load_index()), len(index['records']))
            self.assertEqual([r['case_id'] for r in m.search('NEWCASE')], ['NEWCASE'])

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
        report = self.render(self.article, self.sha, self.spec, self.out)
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
        self.render(self.article, self.sha, spec, self.out)
        self.assertNotIn('<image', (self.out / 'figure.svg').read_text())

    def test_unicode_escape_and_citation_links(self):
        quote = 'Café & tea <review> "yes" — [notes](https://example.org/a?x=1&y=2).'
        spec = m.draft(quote, 'hash')
        spec['title'] = '<script>alert("x")</script> & café'
        self.assertTrue(m.qa(quote, 'hash', spec)['ok'])
        self.render(quote, 'hash', spec, self.out)
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
            self.render(article, 'hash', spec, self.out / family)
            ET.parse(self.out / family / 'figure.svg')
        for family in ('matrix', 'cover'):
            spec = m.draft(self.article, self.sha)
            spec['family'] = family
            if family == 'matrix':
                spec['columns'] = ['Evidence']
                for node in spec['nodes']:
                    node['cells'] = [{'text': 'Exact article excerpt', 'quote': node['quote']}]
            self.render(self.article, self.sha, spec, self.out / family)
            ET.parse(self.out / family / 'figure.svg')

    def test_output_traversal_and_symlinks(self):
        for path in (ROOT / '..' / 'escape', '/tmp/illustration-escape'):
            with self.assertRaisesRegex(ValueError, 'inside Skill'):
                self.render(self.article, self.sha, self.spec, path)
        link = self.out / 'escape'
        link.symlink_to('/tmp', target_is_directory=True)
        with self.assertRaises(ValueError):
            m.write_text(link / 'not-written.txt', 'x')
        child = self.out / 'figure.svg'
        child.symlink_to(ROOT / 'SKILL.md')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.render(self.article, self.sha, self.spec, self.out)
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

    def test_expanded_two_tier_evidence_and_preservation(self):
        cases = m.load_index()
        articles = m.load_catalog()
        self.assertGreaterEqual(len(cases), 80)
        self.assertGreaterEqual(len(articles), 450)
        self.assertGreaterEqual(sum(len(a['figures']) for a in articles), 1800)
        self.assertEqual(len({a['article_url'] for a in articles}), len(articles))
        ids = {r['case_id'] for r in cases}
        self.assertTrue({'A2', 'P2', 'Q1', 'MCP1', 'SKY1', 'X2', 'X7'} <= ids)
        self.assertEqual(sum(r['provenance']['status'] == 'retained_prior_review'
                             for r in cases), 59)
        self.assertTrue(all('inference' in r and r['observed'] and
                            r['analysis']['production_certainty'] == 'uncertain'
                            for r in cases))
        self.assertEqual(len([a for a in articles if a['category'] == 'engineering']) > 0, True)

    def test_catalog_discovery_is_not_visual_approval(self):
        candidates = m.catalog('alignment evaluation', limit=12)
        self.assertTrue(candidates)
        for result in candidates:
            self.assertEqual(result['evidence_level'], 'metadata_only')
            self.assertTrue(m.valid_source_url(result['article_url']))
            self.assertLessEqual(len(result['figures']), 3)
            self.assertGreaterEqual(result['figure_locator_count'], len(result['figures']))
        detail = m.show('X2')
        self.assertEqual(detail['provenance']['status'], 'current_pixel_review')
        self.assertGreaterEqual(len(detail['observed']), 2)
        self.assertEqual(detail['analysis']['production_certainty'], 'uncertain')

    def test_new_figure_reference_in_editable_workflow(self):
        case = m.show('X2')
        edited = copy.deepcopy(self.spec)
        edited['case_refs'] = [{'case_id': 'X2', 'article_url': case['article_url']}]
        report = self.render(self.article, self.sha, edited, self.out)
        self.assertTrue(report['ok'])
        self.assertIn('Design case X2', (self.out / 'preview.html').read_text())
        self.assertNotIn('data:image', (self.out / 'figure.svg').read_text())

    def test_full_article_reviewed_placement_respects_no_image(self):
        parts, selected = m.selected_sections(self.article)
        self.assertGreaterEqual(len(parts), 4)
        target = next(p for p in parts if p['title'] == 'Check the evidence')
        quote = 'The archivist checks the record against the collection catalogue.'
        self.assertIn(quote, target['body'])
        decisions = [
            {'section_id': part['section_id'], 'decision': 'image',
             'family': 'flow', 'reason': 'This section needs a handoff visual.',
             'reader_question': 'How are catalogue results passed for access review?',
             'quotes': [quote], 'case_ids': ['X2']}
            if part['section_id'] == target['section_id']
            else {'section_id': part['section_id'], 'decision': 'no-image',
                  'reason': 'This step is already clear in prose.'}
            for part in parts
        ]
        review = {'article_sha256': self.sha, 'audience': 'Archive reviewers',
                  'thesis': 'Evidence and access decisions must remain traceable.',
                  'reviewer': 'AI semantic author review',
                  'rationale': 'One complex handoff benefits from illustration.',
                  'decisions': decisions}
        plan = m.plan(self.article, self.sha, budget=1, reviewed=review)
        self.assertEqual(plan['approval'].startswith('supplied editorial plan'), True)
        self.assertEqual(len(plan['decisions']), len(parts))
        self.assertEqual(sum(d['decision'] == 'image' for d in plan['decisions']), 1)
        self.assertEqual(m.plan(self.article, self.sha, budget=1,
                                section=target['section_id'], reviewed=review)['selection'],
                         [target['section_id']])
        bad = copy.deepcopy(review)
        bad['decisions'][0]['reason'] = ''
        with self.assertRaises(ValueError):
            m.plan(self.article, self.sha, reviewed=bad)
        bad = copy.deepcopy(review)
        selected_decision = next(d for d in bad['decisions'] if d['decision'] == 'image')
        selected_decision['case_ids'] = ['E8']
        with self.assertRaisesRegex(ValueError, 'negative contrast'):
            m.plan(self.article, self.sha, reviewed=bad)

    def test_retrieval_disambiguates_unreviewed_and_negative(self):
        self.assertIn('X2', [r['case_id'] for r in m.search('X2')])
        self.assertNotIn('E8', [r['case_id'] for r in m.search('', limit=100)])
        self.assertEqual([r['case_id'] for r in m.search('E8', include_contrast=True)],
                         ['E8'])
        with self.assertRaises(ValueError):
            m.show('NO_SUCH_CASE')


if __name__ == '__main__':
    unittest.main()
