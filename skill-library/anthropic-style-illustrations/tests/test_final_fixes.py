"""Targeted dispatch and rendering regressions, entirely offline."""
import contextlib
import io
from unittest.mock import patch
from fixtures import LocalTest
import illustrations
from asi.publication import read_publication
from asi.corpus import Response
from asi.rendering import compose


class FinalFixTests(LocalTest):
    def test_collection_network_dispatch(self):
        for command in ('discover', 'collect'):
            for mode in (None, 'direct', 'system'):
                args = [command, '--allow-network'] + (['--network', mode] if mode else [])
                with patch('asi.corpus.'+command, return_value={}) as call, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(illustrations.main(args), 0)
                self.assertEqual(call.call_args.kwargs['network'], mode or 'direct')
            with patch('asi.corpus.'+command) as call, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(illustrations.main([command, '--network', 'system']), 2)
                call.assert_not_called()

    def test_publication_network_dispatch_and_transport(self):
        url = 'https://www.anthropic.com/engineering/fixture'
        for mode in (None, 'direct', 'system'):
            args = ['publication-context', url, '--allow-network', '--out', str(self.path/'receipt.json')]
            if mode:
                args += ['--network', mode]
            with patch('asi.publication.read_publication', return_value=('# Title\n\nContent.', {})) as call, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(illustrations.main(args), 0)
            self.assertEqual(call.call_args.kwargs['network'], mode or 'direct')
        for mode in ('direct', 'system'):
            with patch('asi.publication.SafeFetcher') as factory:
                factory.return_value.get.return_value = Response(url, 200, {}, b'<article><h1>Title</h1><p>Content.</p></article>')
                _, receipt = read_publication(url, network=mode)
                self.assertEqual(factory.call_args.kwargs['network'], mode)
                self.assertEqual(receipt['transport'], f'SafeFetcher(network={mode})')

    def test_footer_deduplicated(self):
        _, spec = self.spec()
        url = 'https://www.anthropic.com/engineering/building-effective-agents'
        spec['references'] = [{'source_url': url}] * 3
        svg, _ = compose(spec)
        self.assertEqual(svg.count('Building effective agents'), 1)
        self.assertNotIn(url, svg)

    def test_stale_negative_remains_excluded(self):
        from asi import library
        from fixtures import ROOT
        knowledge = ROOT / 'knowledge'
        negative = next(row for row in library.rows(knowledge / 'analyses.jsonl')
                        if row.get('case_id') == 'E8')
        article, figure = library.lookup(knowledge, negative['figure_id'])
        original = library.validate_analysis
        def stale(record, source, occurrence):
            if record.get('case_id') == 'E8':
                raise ValueError('Stale analysis: source hash changed')
            return original(record, source, occurrence)
        with patch('asi.library.validate_analysis', side_effect=stale):
            hits = library.search(knowledge, article['title'], limit=100)
        self.assertNotIn(figure['id'], [hit['figure_id'] for hit in hits])
        self.assertEqual(negative['usage'], 'contrast_not_target')
