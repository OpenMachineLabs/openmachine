"""Behavioral regressions for source-bound curation; no network or pixel claims."""
import copy
import fcntl
import json
from pathlib import Path
from unittest.mock import patch
from fixtures import LocalTest, ROOT, corpus
from asi.library import rows, source_hash, validate_analysis, import_analyses, search, audit
from asi.planning import plan
from asi.rendering import compose
from asi.library import classification


class CurationTests(LocalTest):
    def test_stable_refresh_and_meaningful_changes(self):
        a, f = self.corpus_fixture()
        original = source_hash(a, f)
        a.update(retrieved_at='later', response_sha256='changed HTTP wrapper')
        f['observed_at'] = 'later'
        self.assertEqual(source_hash(a, f), original)
        for target, key, value in [(a, 'content_sha256', 'new content'), (f, 'caption', 'new meaning'),
                                    (f, 'source_locator', {'page': 2}), (f, 'url', 'https://example.org/new.png')]:
            aa, ff = copy.deepcopy(a), copy.deepcopy(f)
            (aa if target is a else ff)[key] = value
            self.assertNotEqual(source_hash(aa, ff), original)

    def test_paraphrase_quotes_and_wrong_sources(self):
        a, f = self.corpus_fixture()
        r = self.analysis(a, f)
        item = {'claim':'Context paraphrase', 'locator':a['url'], 'method':'context_review_paraphrase',
                'context_review':{'source_hash':r['source_hash'], 'locator':a['url'], 'reviewer':'fixture',
                                  'date':'2025-01-01', 'notes_origin':'fixture'}}
        r['evidence']['source_stated'] = [item]
        validate_analysis(r, a, f)
        for key, value in [('locator', 'https://www.anthropic.com/wrong'), ('quote', 'fabricated quotation')]:
            bad = copy.deepcopy(r); bad['evidence']['source_stated'][0][key] = value
            with self.assertRaises(ValueError): validate_analysis(bad, a, f)
        item['quote'] = f['caption']; validate_analysis(r, a, f)
        del item['quote']; item['context_review']['source_hash'] = 'wrong'
        with self.assertRaises(ValueError): validate_analysis(r, a, f)

    def test_pixel_method_and_locator(self):
        a, f = self.corpus_fixture()
        for change in ({'method':'source_markup'}, {'locator':'https://www.anthropic.com/wrong.png'}, {'method':'unknown'}):
            r = self.analysis(a, f); r['pixel_inspection'].update(change)
            with self.assertRaises(ValueError): validate_analysis(r, a, f)
        a['url'] = 'https://arxiv.org/pdf/1234.56789'; a['source_id'] = 'linked_paper'
        r = self.analysis(a, f)
        with self.assertRaises(ValueError): validate_analysis(r, a, f)
        a['official_provenance'] = {'paper_url':a['url'], 'article_url':'https://www.anthropic.com/research/test',
                                  'article_content_sha256':'fixture', 'authorship_verified':True,
                                  'method':'official_article_link_and_paper_authors'}
        validate_analysis(r, a, f)
        self.assertEqual(classification(a), 'verified_official_paper')
        a['official_provenance']['authorship_verified'] = False
        self.assertEqual(classification(a), 'supporting_literature')

    def test_upsert_idempotency_history_and_duplicates(self):
        a, f = self.corpus_fixture()
        second = {**f, 'id':'second', 'asset_id':'second-asset'}
        self.index('figures', [f, second])
        old = self.analysis(a, f); other = self.analysis(a, second)
        self.index('analyses', [old, other]); self.index('incoming', [old])
        dest = self.path/'analyses.jsonl'; before = dest.read_bytes()
        self.assertEqual(import_analyses(self.path,self.path/'incoming.jsonl',dest)['changed'],0)
        self.assertEqual(dest.read_bytes(), before)
        changed = {**old, 'editorial_purpose':'Revised original judgment'}
        self.index('incoming', [changed])
        import_analyses(self.path,self.path/'incoming.jsonl',dest)
        retained = list(rows(dest)); self.assertEqual(len(retained),2)
        self.assertEqual(retained[0]['replacement_history'],[old]); self.assertEqual(retained[1],other)
        before = dest.read_bytes(); import_analyses(self.path,self.path/'incoming.jsonl',dest)
        self.assertEqual(dest.read_bytes(),before)
        self.index('incoming', [changed,changed])
        with self.assertRaises(ValueError): import_analyses(self.path,self.path/'incoming.jsonl',dest)
        self.assertEqual(dest.read_bytes(),before)
        self.index('incoming', [changed]); self.index('analyses', [old,old])
        with self.assertRaises(ValueError): import_analyses(self.path,self.path/'incoming.jsonl',dest)

    def test_import_lock_and_atomic_failure(self):
        a,f = self.corpus_fixture(); r = self.analysis(a,f)
        self.index('analyses',[r]); self.index('incoming',[{**r,'editorial_purpose':'Changed'}])
        dest = self.path/'analyses.jsonl'; before = dest.read_bytes()
        with (self.path/'analyses.jsonl.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): import_analyses(self.path,self.path/'incoming.jsonl',dest)
        with patch('asi.library.os.fsync',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError): import_analyses(self.path,self.path/'incoming.jsonl',dest)
        self.assertEqual(dest.read_bytes(),before)

    def test_audit_real_counts_duplicates_and_zero_data(self):
        self.assertEqual(len(audit(self.path)['errors']),4)
        a,f = self.corpus_fixture(); valid = self.analysis(a,f); stale = {**valid,'source_hash':'old'}
        self.index('analyses',[valid,stale])
        report = audit(self.path)
        self.assertEqual((report['valid_analyses'],report['stale_analyses']),(1,1))
        self.assertEqual(report['duplicate_ids']['analyses'],[f['id']])
        self.assertFalse(report['visual_review']['independently_endorsed'])

    def test_landing_and_negative_exclusion(self):
        a,f = self.corpus_fixture(); a['document_kind'] = 'landing_page'; self.index('articles',[a])
        self.assertEqual(search(self.path,'routing'),[])
        self.assertTrue(search(self.path,'routing',include_supporting=True))
        a['document_kind'] = 'html_article'; self.index('articles',[a])
        r = self.analysis(a,f); r['usage'] = 'contrast_not_target'; self.index('analyses',[r])
        self.assertEqual(search(self.path,'routing'),[])
        self.assertEqual(audit(self.path)['negative_contrasts'],1)

    def test_manual_pdf_region_survives_refresh_and_stales(self):
        import fitz
        url = 'https://arxiv.org/pdf/1234.56789'
        with fitz.open() as doc:
            doc.new_page().insert_text((40,40),'Original document')
            body = doc.tobytes()
        class Client:
            def get(self,target,headers=None): return corpus.Response(url,200,{'content-type':'application/pdf'},body)
        corpus.collect(self.path,urls=[url],fetcher=Client(),include_pdfs=True,progress=False)
        a = next(rows(self.path/'articles.jsonl'))
        f = {'id':'manual','article_id':a['id'],'asset_id':'manual-asset','url':url,'display_url':url+'#page=1',
             'kind':'manual_pdf_region','source_locator':{'page':1,'region':'upper panel'}}
        self.index('figures',[f]); self.index('assets',[{'id':'manual-asset'}]); old_hash = source_hash(a,f)
        corpus.collect(self.path,urls=[url],fetcher=Client(),include_pdfs=True,refresh=True,progress=False)
        self.assertEqual(next(rows(self.path/'figures.jsonl')),f)
        self.assertEqual(source_hash(next(rows(self.path/'articles.jsonl')),f),old_hash)
        with fitz.open() as doc:
            doc.new_page().insert_text((40,40),'Changed scientific content'); body=doc.tobytes()
        corpus.collect(self.path,urls=[url],fetcher=Client(),include_pdfs=True,refresh=True,progress=False)
        self.assertEqual(next(rows(self.path/'figures.jsonl')),f)
        self.assertNotEqual(source_hash(next(rows(self.path/'articles.jsonl')),f),old_hash)

    def test_cross_topic_structures(self):
        queries = [('fork parallel independent work aggregation','A4'), ('routing classification exclusive dispatch','A3'),
                   ('feedback iteration critique acceptance','A6'), ('population comparison share employment usage','F4')]
        for query, expected in queries:
            found = search(ROOT/'knowledge',query,reviewed_only=True,limit=3)
            self.assertIn(expected,[r['analysis']['case_id'] for r in found],query)
            self.assertNotEqual(found[0]['analysis']['case_id'],'E8')
        self.assertEqual(search(ROOT/'knowledge','how does the routing classification exclusive dispatch')[0]['analysis']['case_id'],'A3')

    def test_full_section_context_and_no_image(self):
        text='# Background\nThis proposal has no measurements.\n## Mechanism\nCompare two methods.\n## Limits\nNo causal claims are justified.'
        p=plan(text,section='Mechanism',budget=1)
        self.assertEqual(p['full_context'],text);self.assertEqual(len(p['sections']),3)
        self.assertEqual([d['decision'] for d in p['decisions']],['no_image','illustrate','no_image'])
        self.assertTrue(all(d['decision']=='no_image' for d in plan('# Note\nThank you for reading.')['decisions']))

    def test_distinct_palette_options(self):
        _,spec=self.spec()
        spec['palette']='engineering'; green,_=compose(spec)
        spec['palette']='scientific'; neutral,_=compose(spec)
        self.assertIn('#286b4b',green); self.assertIn('#202630',neutral)
        self.assertNotEqual(green,neutral)

    def test_whole_publication_parser_retains_all_editorial_sections_transiently(self):
        from asi.publication import read_publication,coverage
        url='https://www.anthropic.com/engineering/fixture'
        class Client:
            calls=[]
            def get(self,target):
                self.calls.append(target)
                return corpus.Response(url,200,{},b'<main><nav>Navigation</nav><h1>Title</h1><p>Opening.</p><h2>Mechanism</h2><p>Compare alternatives.</p><h2>Limit</h2><p>No measurements.</p><h2>Appendix</h2><ul><li>Interface requirement.</li></ul><footer>Footer</footer></main>')
        client=Client();text,receipt=read_publication(url,fetcher=client)
        self.assertEqual(client.calls,[url]);self.assertIn('Interface requirement.',text)
        self.assertNotIn('Navigation',text);self.assertNotIn('Footer',text)
        self.assertEqual(len(coverage(text)),4)
        self.assertNotIn('Opening.',json.dumps(coverage(text)))
        selected=plan(text,section='Mechanism')
        self.assertIn('No measurements.',selected['full_context'])
        self.assertFalse(receipt['full_source_retained'])

    def test_original_notes_preserved_without_extra_reviews(self):
        retained={r['case_id']:r for r in rows(ROOT/'knowledge/analyses.jsonl')}
        seen=[]
        for path in sorted((ROOT/'runs').glob('curation-0[1-5].json')):
            for group in json.loads(path.read_text())['groups']:
                for study in group['studies']:
                    seen.append(study['key']);self.assertEqual(retained[study['key']]['original_notes']['study'],study)
        if seen:
            self.assertEqual(len(seen),59);self.assertEqual(len(set(seen)),59)
        self.assertEqual(len(retained),59)
        self.assertTrue(all(r['case_id']==r['original_notes']['study']['key'] for r in retained.values()))

    def test_pdf_review_rejects_wrong_page_region_and_context_shape(self):
        a,f=self.corpus_fixture();f['source_locator']={'page':2,'region':'upper panel'}
        r=self.analysis(a,f);r['pixel_inspection'].update(method='official_pdf_screenshot',page=2,region='upper panel')
        validate_analysis(r,a,f)
        for change in ({'page':7},{'region':'lower panel'}):
            bad=copy.deepcopy(r);bad['pixel_inspection'].update(change)
            with self.assertRaises(ValueError):validate_analysis(bad,a,f)
        r['evidence']['source_stated']=[{'claim':'fixture','locator':a['url'],'method':'context_review_paraphrase','context_review':[]}]
        with self.assertRaises(ValueError):validate_analysis(r,a,f)
