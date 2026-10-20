"""Shared offline fixtures; no test cases are collected here."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from asi import corpus
from asi.library import source_hash
from asi.examples import base_spec

class LocalTest(unittest.TestCase):
    def setUp(self):
        (ROOT/'runs').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'runs')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.block = patch('socket.create_connection', side_effect=AssertionError('Tests must not use the network'))
        self.block.start()
        self.addCleanup(self.block.stop)

    def index(self, name, values):
        (self.path/(name+'.jsonl')).write_text(''.join(json.dumps(v)+'\n' for v in values), encoding='utf-8')

    def corpus_fixture(self):
        articles, figures, assets = corpus.parse_article(b'<html><h1>Routing workflow</h1><article><h2>Routing</h2><figure><img src="/routing.png" width="800"><figcaption>A routing workflow</figcaption></figure></article></html>', 'https://www.anthropic.com/engineering/routing', 'anthropic')
        self.index('articles', [articles])
        self.index('figures', figures)
        self.index('assets', assets)
        return articles, figures[0]

    def analysis(self, a, f):
        return {'figure_id': f['id'], 'article_id': a['id'], 'source_hash': source_hash(a,f), 'family':'flow',
                'relationship':'selection', 'editorial_purpose':'Test-only synthetic fixture', 'information_structure':'Test branching',
                'composition':'Fixture description', 'palette':'Fixture palette', 'typography':'Fixture type',
                'production_hypothesis':'Fixture reconstruction', 'production_certainty':'uncertain',
                'evidence':{'observed':[{'claim':'Fixture only, not actual inspection', 'locator':f['url']}], 'source_stated':[], 'inferred':[]},
                'pixel_inspection':{'locator':f['url'], 'date':'2025-01-01', 'inspector':'automated test fixture', 'method':'official_image_pixels'},
                'reusable_rules':['Test rule'], 'counterexamples':['Test counterexample'],
                'cross_domain_transfer':{'domain':'test', 'mapping':'test mapping', 'changes':'test changes', 'limits':'test only'}}

    def spec(self, family='cover'):
        text = '# Input\n\nAlpha has 10 units. Beta has 20 units.\n'
        spec = base_spec(text,'Original figure',family,'section-1','Alpha has 10 units. Beta has 20 units.')
        spec['items'] = [{'id':'alpha', 'label':'Alpha', 'claim_ids':['c1']}]
        return text,spec
