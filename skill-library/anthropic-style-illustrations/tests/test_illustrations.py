"""Offline behavior tests; every disposable file lives inside the Skill."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from asi import corpus
from asi.library import audit, digest, import_analyses, lookup, output_path, packet, rows, search, source_hash, validate_analysis, write_json
from asi.planning import plan, sections, validate_spec
from asi.rendering import compose, qa, render, wrap
from asi.examples import base_spec


from fixtures import LocalTest


class ParserTests(LocalTest):
    def test_html_context_and_dedup(self):
        body = b'<html><h1>Title</h1><article><h2>Section</h2><p>Context</p><figure><img src="/x.png"><figcaption>Caption</figcaption></figure><img src="/x.png"><script>alert(1)</script></article></html>'
        a, f, assets = corpus.parse_article(body,'https://www.anthropic.com/engineering/test','anthropic')
        self.assertEqual((len(f),len(assets)),(2,1))
        self.assertNotEqual(f[0]['id'],f[1]['id'])
        self.assertEqual(f[0]['caption'],'Caption')
        self.assertNotIn('alert',json.dumps(a))
        self.assertLessEqual(a['excerpt_word_count'],150)

    def test_embedded_and_lazy_media(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new('RGB',(100,80)).save(stream,format='PNG')
        data = base64.b64encode(stream.getvalue()).decode()
        a, f, assets = corpus.parse_article(f'<article><img src="data:image/png;base64,{data}"></article>'.encode(), 'https://www.anthropic.com/engineering/test')
        self.assertEqual(f[0]['kind'],'embedded_image')
        self.assertEqual(assets[0]['embedded_metadata']['width'],100)
        self.assertNotIn(data,json.dumps(assets))
        _, f, _ = corpus.parse_article(b'<article><img src="data:image/png;base64,bad" data-src="/real.png"></article>','https://www.anthropic.com/engineering/test')
        self.assertTrue(f[0]['url'].endswith('/real.png'))

    def test_proxy_and_unsafe_url(self):
        self.assertEqual(corpus.normalize_url('https://www.anthropic.com/_next/image?url=%2Fx.png&w=200', unwrap_image=True),'https://www.anthropic.com/x.png')
        for value in ('javascript:alert(1)', 'https://user:pass@example.org/a', 'https://example.org:444/a'):
            with self.assertRaises(ValueError):corpus.normalize_url(value)

    def test_xml_entity_rejection(self):
        with self.assertRaises(ValueError):corpus._links_from_index(b' ' * 6000+b'<!ENTITY x "y">','https://example.org')

    def test_pdf_caption_and_page_bound(self):
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((40,40),'Figure 1: A source caption')
            a,f,_ = corpus.parse_pdf(document.tobytes(),'https://arxiv.org/pdf/1234.56789','linked_paper')
        self.assertEqual(len(f),1)
        self.assertEqual(f[0]['source_locator']['page'],1)
        self.assertEqual(a['source_id'],'linked_paper')

    def test_public_dns_guard(self):
        client = corpus.SafeFetcher({'example.org'})
        with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            with self.assertRaises(corpus.FetchError):client.validate('https://example.org')
            with self.assertRaises(OSError):corpus.PublicHTTPSConnection('example.org').connect()


class JournalTests(LocalTest):
    def test_torn_tail_recovery(self):
        journal = self.path/'.collector-journal.jsonl'
        journal.write_text(json.dumps({'upserts':{'articles':[{'id':'a'}]}})+'\n{"upserts":')
        store = corpus.Store(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store.tables['articles']['a'],{'id':'a'})
        self.assertEqual(journal.read_text(),'')

    def test_complete_corruption_rejected(self):
        (self.path/'.collector-journal.jsonl').write_text('{bad}\n')
        with self.assertRaises(ValueError):corpus.Store(self.path)

    def test_idempotent_replay_and_lock(self):
        store = corpus.Store(self.path)
        self.addCleanup(store.close)
        with self.assertRaises(RuntimeError):corpus.Store(self.path)
        tx = {'upserts':{'articles':[{'id':'a','value':1}]}}
        store.commit(tx)
        store.commit(tx)
        self.assertEqual(len(store.tables['articles']),1)
        with self.assertRaises(ValueError):store.commit({'upserts':{'unknown':[]}})
        self.assertEqual(len((self.path/'.collector-journal.jsonl').read_text().splitlines()),2)

    def test_failed_refresh_retries_without_losing_snapshot(self):
        url = 'https://www.anthropic.com/engineering/test'
        class Client:
            calls = 0
            def get(self, target, headers=None):
                self.calls += 1
                if self.calls == 2:raise corpus.FetchError('temporary',retryable=True)
                return corpus.Response(url,200,{'content-type':'text/html'},b'<html><h1>Retained</h1></html>')
        client=Client()
        config={'sources':[{'id':'anthropic','hosts':['www.anthropic.com'],'article_paths':['^/engineering/']} ]}
        corpus.collect(self.path,urls=[url],sources=config,fetcher=client,progress=False)
        failed=corpus.collect(self.path,urls=[url],sources=config,fetcher=client,refresh=True,progress=False)
        self.assertEqual(failed['failed'],1)
        self.assertEqual(len(list(rows(self.path/'articles.jsonl'))),1)
        recovered=corpus.collect(self.path,urls=[url],sources=config,fetcher=client,progress=False)
        self.assertEqual(recovered['fetched'],1)
        self.assertEqual(recovered['open_failures'],0)

    def test_orphan_304_is_not_success(self):
        class Client:
            def get(self,url,headers=None):return corpus.Response(url,304,{},b'')
        result=corpus.collect(self.path,urls=['https://www.anthropic.com/engineering/test'],fetcher=Client(),progress=False)
        self.assertEqual(result['failed'],1)
        self.assertEqual(result['articles'],0)

    def test_pdf_fanout_bound(self):
        class Client:
            calls=[]
            def get(self,url,headers=None):
                self.calls.append(url)
                if url.endswith('.pdf'):raise corpus.FetchError('fixture PDF unavailable')
                return corpus.Response(url,200,{'content-type':'text/html'},('<html><article>'+''.join(f'<a href="https://www-cdn.anthropic.com/{i}.pdf">paper</a>' for i in range(10))+'</article></html>').encode())
        client=Client()
        result=corpus.collect(self.path,urls=['https://www.anthropic.com/engineering/test'],fetcher=client,include_pdfs=True,max_linked_pdfs=2,progress=False)
        self.assertEqual(len(client.calls),3)
        self.assertEqual(result['linked_pdfs_enqueued'],2)


class RetrievalTests(LocalTest):
    def test_bm25_and_asset_dedup(self):
        a,f=self.corpus_fixture()
        other=copy.deepcopy(f)
        other['id']='duplicate'
        other['caption']='routing routing routing'
        self.index('figures',[f,other])
        found=search(self.path,'routing')
        self.assertEqual(len(found),1)
        self.assertGreater(found[0]['score'],0)
        self.assertEqual(search(self.path,'absent'),[])

    def test_supporting_exclusion(self):
        a,f=self.corpus_fixture()
        a['source_id']='linked_paper'
        self.index('articles',[a])
        self.assertEqual(search(self.path,'routing'),[])
        self.assertEqual(search(self.path,'routing',include_supporting=True)[0]['classification'],'supporting_literature')
        self.assertEqual(audit(self.path)['supporting_literature'],1)

    def test_analysis_shape_and_stale_rejection(self):
        a,f=self.corpus_fixture()
        r=self.analysis(a,f)
        validate_analysis(r,a,f)
        self.index('analyses',[r])
        self.assertEqual(search(self.path,'routing',reviewed_only=True)[0]['evidence_level'],'validated_analysis')
        r['source_hash']='wrong'
        self.index('analyses',[r])
        self.assertEqual(search(self.path,'routing',reviewed_only=True),[])
        self.assertEqual(search(self.path,'routing')[0]['evidence_level'],'metadata_only')

    def test_filters_and_relevance_first(self):
        a,f=self.corpus_fixture()
        second=copy.deepcopy(f)
        second.update(id='second',asset_id='second-asset',caption='routing '*40)
        self.index('figures',[f,second])
        self.index('analyses',[self.analysis(a,f)])
        self.assertEqual(search(self.path,'routing')[0]['figure_id'],second['id'])
        self.assertEqual(len(search(self.path,'routing',family='flow',relationship='selection')),1)
        self.assertEqual(search(self.path,'routing',family='bar'),[])

    def test_import_does_not_endorse(self):
        a,f=self.corpus_fixture()
        self.index('incoming',[self.analysis(a,f)])
        result=import_analyses(self.path,self.path/'incoming.jsonl',self.path/'validated.jsonl')
        self.assertFalse(result['endorsement'])
        self.assertEqual(result['validated'],1)
        bad=self.analysis(a,f)
        bad['evidence']['observed']=['generic text']
        self.index('incoming',[bad])
        with self.assertRaises(ValueError):import_analyses(self.path,self.path/'incoming.jsonl',self.path/'bad.jsonl')
        self.assertFalse((self.path/'bad.jsonl').exists())

    def test_packet_context(self):
        a,f=self.corpus_fixture()
        result=packet(self.path,f['id'])
        self.assertEqual(result['article_id'],a['id'])
        self.assertTrue(result['image_link'].startswith('https://'))
        self.assertEqual(result['inspection_status'],'pending_host_multimodal_inspection')


class PlanningTests(LocalTest):
    def test_fences_setext_and_all_sections(self):
        text='Title\n=====\n\n## Flow\nworkflow text\n```md\n# not a heading\n```\n\nConclusion\n----------\nPlain prose\n'
        parts=sections(text)
        self.assertEqual([p['heading'] for p in parts],['Title','Flow','Conclusion'])
        result=plan(text,section='Flow',budget=1)
        self.assertEqual(result['full_context'],text)
        self.assertEqual(len(result['decisions']),3)
        self.assertEqual(result['decisions'][-1]['decision'],'no_image')
        self.assertEqual(result['input_hash'],digest(text))

    def test_long_and_non_english(self):
        text='# \u6d41\u7a0b\n'+('\u6b65\u9aa4\u5185\u5bb9\u3002'*5000)+'\n## End\nConclusion'
        result=plan(text,budget=0)
        self.assertEqual(result['full_context'],text)
        self.assertTrue(all(d['decision']=='no_image' for d in result['decisions']))
        self.assertEqual(len(sections(text)),2)

    def test_reviewed_plan_binding(self):
        text='# Flow\nworkflow details\n## End\nProse'
        reviewed=plan(text,budget=1)
        reviewed['review']={'reviewer':'test fixture','date':'2025-01-01','rationale':'fixture semantic receipt'}
        result=plan(text,budget=1,reviewed=reviewed)
        self.assertEqual(result['status'],'reviewed_semantic_plan')
        with self.assertRaises(ValueError):plan(text+'changed',reviewed=reviewed)
        reviewed['decisions'].pop()
        with self.assertRaises(ValueError):plan(text,reviewed=reviewed)

    def test_unknown_section_and_budget(self):
        with self.assertRaises(ValueError):plan('# A\nText',section='missing')
        with self.assertRaises(ValueError):plan('# A',budget=-1)


class RendererTests(LocalTest):
    def test_deterministic_exports(self):
        text,spec=self.spec()
        render(spec,text,self.path,self.path/'one')
        render(spec,text,self.path,self.path/'two')
        for name in ('figure.svg','figure.json','preview.html','figure.png','qa.json'):
            self.assertEqual((self.path/'one'/name).read_bytes(),(self.path/'two'/name).read_bytes(),name)
        self.assertEqual(qa(spec,text,self.path,self.path/'one')['visual_review'],'NOT RUN')
        (self.path/'one/figure.svg').write_text('<svg/>')
        with self.assertRaises(ValueError):qa(spec,text,self.path,self.path/'one')

    def test_escape_and_injection(self):
        text,spec=self.spec()
        spec['title']='A < B & "C"'
        svg,_=compose(spec)
        self.assertIn('A &lt; B &amp;',svg)
        for injection in ('<script>alert(1)</script>','javascript:alert(1)','file:///etc/passwd','https://u:p@example.com/a'):
            spec['title']=injection
            with self.assertRaises(ValueError):compose(spec)

    def test_duplicate_and_missing_data(self):
        text,spec=self.spec()
        spec['items']*=2
        with self.assertRaises(ValueError):compose(spec)
        spec['family']='bar'
        with self.assertRaises(ValueError):compose(spec)

    def test_chart_evidence_and_units(self):
        text,spec=self.spec('bar')
        spec.pop('items')
        spec.update(unit='units',data=[{'id':'a','label':'Alpha','value':10,'evidence':'Alpha has 10 units.','claim_ids':['c1']}])
        render(spec,text,self.path,self.path/'chart')
        for number in (-1, float('nan'),float('inf'), True, 20, 99):
            spec['data'][0]['value']=number
            with self.assertRaises(ValueError):compose(spec)
        spec['data'][0]['value']=10
        spec['unit']='percent'
        with self.assertRaises(ValueError):compose(spec)

    def test_line_x_and_order(self):
        text='# Series\nA: 1 days, 10 units. B: 2 days, 20 units.'
        spec=base_spec(text,'Series','line','section-1','A: 1 days, 10 units. B: 2 days, 20 units.')
        spec.update(unit='units',x_unit='days',data=[{'id':'a','label':'A','x':1,'value':10,'evidence':'A: 1 days, 10 units.','claim_ids':['c1']},{'id':'b','label':'B','x':2,'value':20,'evidence':'B: 2 days, 20 units.','claim_ids':['c1']}])
        render(spec,text,self.path,self.path/'line')
        spec['data'].reverse()
        with self.assertRaises(ValueError):compose(spec)
        spec['data'].reverse()
        facts = []
        for item, x in zip(spec['data'], (1e306, 2e306)):
            item['x'] = x
            item['evidence'] = f'{item["label"]}: {x:.0f} days, {item["value"]} units.'
            facts.append(item['evidence'])
        spec['claims'][0]['quote'] = ' '.join(facts)
        svg, _ = compose(spec)
        self.assertNotIn('cx="inf"', svg)
        self.assertNotIn('nan', svg)


    def test_fork_join_loop(self):
        text,spec=self.spec('flow')
        spec.pop('items')
        spec['nodes']=[{'id':i,'label':i,'row':r,'column':c,'claim_ids':['c1']} for i,r,c in [('start',0,0),('upper',0,1),('lower',1,1),('join',0,2)]]
        spec['edges']=[{'from':a,'to':b,'meaning':'proposed handoff','claim_ids':['c1']} for a,b in [('start','upper'),('start','lower'),('upper','join'),('lower','join'),('join','start')]]
        svg,report=compose(spec)
        self.assertEqual(report['nodes'],4)
        self.assertEqual(svg.count('<polyline'),5)
        spec['edges'][0]['to']='missing'
        with self.assertRaises(ValueError):compose(spec)

    def test_flow_rejects_crossing_nodes(self):
        _,spec=self.spec('flow')
        spec['nodes']=[{'id':i,'label':i,'row':r,'column':c,'claim_ids':['c1']} for i,r,c in [('a',0,0),('b',0,1),('c',1,0)]]
        spec['edges']=[{'from':'a','to':'c','meaning':'handoff','claim_ids':['c1']}]
        # Adjacent-row same-column loop uses outside lanes and stays clear.
        compose(spec)
        spec['nodes'][1]['row']=0
        spec['nodes'][1]['column']=0
        with self.assertRaises(ValueError):compose(spec)

    def test_no_truncation_overflow(self):
        words='A long label with meaningful words '*20
        lines=wrap(words,220,18)
        self.assertEqual(' '.join(lines),words.strip())
        _,spec=self.spec()
        spec['items'][0]['label']='x '*100000
        with self.assertRaises(ValueError):compose(spec)

    def test_provenance_rejects_stale_input(self):
        text,spec=self.spec()
        with self.assertRaises(ValueError):validate_spec(spec,text+'changed',self.path)
        spec['claims'][0]['quote']='not in source'
        with self.assertRaises(ValueError):validate_spec(spec,text,self.path)

    def test_reference_binding(self):
        a,f=self.corpus_fixture()
        text,spec=self.spec()
        spec['references']=[{'figure_id':f['id'],'article_id':a['id'],'source_hash':'stale','source_url':a['url'],'transfer':'test','evidence_level':'metadata_only'}]
        with self.assertRaises(ValueError):validate_spec(spec,text,self.path)

    def test_path_traversal_symlink_and_protected_analysis(self):
        self.assertEqual(output_path(ROOT/'knowledge/analyses.jsonl'),ROOT/'knowledge/analyses.jsonl')
        for path in (ROOT/'../escape.json',):
            with self.assertRaises(ValueError):output_path(path)
        (self.path/'outside').symlink_to(ROOT.parent,target_is_directory=True)
        with self.assertRaises(ValueError):write_json(self.path/'outside/escape.json',{})


class CLITests(LocalTest):
    def run_cli(self,*args):
        return subprocess.run([sys.executable,'-B',str(ROOT/'scripts/illustrations.py'),*map(str,args)],cwd=ROOT,capture_output=True,text=True)

    def test_help_and_offline_guard(self):
        self.assertEqual(self.run_cli('--help').returncode,0)
        for name in ('discover','collect'):
            result=self.run_cli(name)
            self.assertEqual(result.returncode,2)
            self.assertIn('allow-network',result.stderr)

    def test_plan_render_qa(self):
        text,spec=self.spec()
        article=self.path/'article.md'
        article.write_text(text)
        write_json(self.path/'spec.json',spec)
        self.assertEqual(self.run_cli('plan',article,'--out',self.path/'plan.json').returncode,0)
        result=self.run_cli('render',self.path/'spec.json','--article',article,'--out',self.path/'rendered')
        self.assertEqual(result.returncode,0,result.stderr)
        result=self.run_cli('qa',self.path/'spec.json','--article',article,'--out',self.path/'rendered')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_search_packet_import_audit(self):
        a,f=self.corpus_fixture()
        for args in [('audit',),('search','routing'),('show',f['id']),('analyze-packet',f['id'],'--out',self.path/'packet.json')]:
            result=self.run_cli('--knowledge',self.path,*args)
            self.assertEqual(result.returncode,0,result.stderr)
        self.index('incoming',[self.analysis(a,f)])
        result=self.run_cli('--knowledge',self.path,'import-analyses',self.path/'incoming.jsonl','--out',self.path/'imported.jsonl')
        self.assertEqual(result.returncode,0,result.stderr)

class FailureRecoveryTests(LocalTest):
    def test_snapshot_failure_retains_journal(self):
        store=corpus.Store(self.path)
        store.commit({'upserts':{'articles':[{'id':'retained'}]}})
        original=(self.path/'.collector-journal.jsonl').read_bytes()
        with patch('asi.corpus._atomic_json',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(RuntimeError):store.flush()
        with self.assertRaises(RuntimeError):store.flush()
        self.assertEqual((self.path/'.collector-journal.jsonl').read_bytes(),original)
        store.close()
        restored=corpus.Store(self.path)
        self.addCleanup(restored.close)
        self.assertIn('retained',restored.tables['articles'])

    def test_commit_failure_does_not_truncate_journal(self):
        store=corpus.Store(self.path)
        with patch('asi.corpus.os.fsync',side_effect=OSError('fixture fsync failure')):
            with self.assertRaises(RuntimeError):store.commit({'upserts':{'articles':[{'id':'recoverable'}]}})
        with self.assertRaises(RuntimeError):store.flush()
        self.assertIn('recoverable',(self.path/'.collector-journal.jsonl').read_text())
        store.close()
        restored=corpus.Store(self.path)
        self.addCleanup(restored.close)
        self.assertIn('recoverable',restored.tables['articles'])

    def test_304_resolves_previous_failure(self):
        url='https://www.anthropic.com/engineering/test'
        class Client:
            def get(self,target,headers=None):return corpus.Response(url,200,{'content-type':'text/html','etag':'v1'},b'<html><h1>Title</h1></html>')
        corpus.collect(self.path,urls=[url],fetcher=Client(),progress=False)
        failure={'id':corpus.stable_id('failure','collection|'+url)}
        self.index('failures',[failure])
        class Unchanged:
            def get(self,target,headers=None):return corpus.Response(url,304,{},b'')
        result=corpus.collect(self.path,urls=[url],fetcher=Unchanged(),refresh=True,progress=False)
        self.assertEqual(result['open_failures'],0)
        self.assertEqual(result['unchanged'],1)


class DiscoveryTests(LocalTest):
    def test_discovery_failure_recovery_and_literature_classification(self):
        config={'allowed_hosts':['arxiv.org'], 'sources':[{'id':'anthropic','hosts':['www.anthropic.com'],
            'discovery_urls':['https://www.anthropic.com/sitemap.xml'], 'article_paths':['^/engineering/']}]}
        class Client:
            fail=True
            def get(self,url,headers=None):
                if self.fail:raise corpus.FetchError('fixture unavailable')
                return corpus.Response(url,200,{},b'<urlset><url><loc>https://www.anthropic.com/engineering/test</loc></url><url><loc>https://arxiv.org/abs/1234.56789</loc></url></urlset>')
        client=Client()
        failed=corpus.discover(config,self.path,fetcher=client,progress=False)
        self.assertEqual(failed['indexes_failed'],1)
        client.fail=False
        recovered=corpus.discover(config,self.path,fetcher=client,progress=False)
        self.assertEqual(recovered['discovered_articles'],2)
        self.assertEqual(list(rows(self.path/'failures.jsonl')),[])
        paper=next(r for r in rows(self.path/'discovery.jsonl') if 'arxiv.org' in r['url'])
        self.assertEqual(paper['source_id'],'linked_paper')

    def test_malformed_analysis_shapes(self):
        a,f=self.corpus_fixture()
        for field in ('evidence','pixel_inspection','cross_domain_transfer'):
            value=self.analysis(a,f)
            value[field]=[]
            with self.assertRaises(ValueError):validate_analysis(value,a,f)

    def test_hash_asset_deduplication(self):
        a,f=self.corpus_fixture()
        other=copy.deepcopy(f)
        other.update(id='other',asset_id='other-asset')
        self.index('figures',[f,other])
        self.index('assets',[{'id':f['asset_id'],'content_sha256':'same-content'},{'id':'other-asset','content_sha256':'same-content'}])
        self.assertEqual(len(search(self.path,'routing')),1)


if __name__=='__main__':
    unittest.main()
