#!/usr/bin/env python3
"""Local article illustration tools. Network collection requires an explicit flag."""
import argparse
import json
from pathlib import Path
import sys

from asi.library import ROOT, ANALYSIS_FAMILIES, audit, import_analyses, packet, search, write_json
from asi.planning import plan
from asi.rendering import qa, render


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--knowledge', type=Path, default=ROOT/'knowledge')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('discover', 'collect'):
        p = sub.add_parser(name)
        p.add_argument('--allow-network', action='store_true')
        p.add_argument('--network', choices=('direct', 'system'), default='direct')
        p.add_argument('--sources', type=Path, default=ROOT/'knowledge/sources.json')
        p.add_argument('--limit', type=int, default=20)
        p.add_argument('--refresh', action='store_true')
        if name == 'collect':
            p.add_argument('--url', action='append')
            p.add_argument('--include-pdfs', action='store_true')
            p.add_argument('--max-linked-pdfs', type=int, default=20)
    sub.add_parser('audit')
    p = sub.add_parser('publication-context')
    p.add_argument('url')
    p.add_argument('--allow-network', action='store_true')
    p.add_argument('--network', choices=('direct', 'system'), default='direct')
    p.add_argument('--section')
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('search')
    p.add_argument('query')
    p.add_argument('--limit', type=int, default=8)
    p.add_argument('--family', choices=ANALYSIS_FAMILIES)
    p.add_argument('--relationship')
    p.add_argument('--reviewed-only', action='store_true', help='Shape-validated current analyses, not independent endorsement')
    p.add_argument('--include-supporting', action='store_true')
    p.add_argument('--analyses', type=Path)
    for name in ('show', 'analyze-packet'):
        p = sub.add_parser(name)
        p.add_argument('figure_id')
        p.add_argument('--out', type=Path)
    p = sub.add_parser('import-analyses')
    p.add_argument('source', type=Path)
    p.add_argument('--out', type=Path, default=ROOT/'knowledge/analyses.jsonl')
    p = sub.add_parser('plan')
    p.add_argument('article', type=Path)
    p.add_argument('--budget', type=int, default=3)
    p.add_argument('--section')
    p.add_argument('--reviewed', type=Path)
    p.add_argument('--out', type=Path, required=True)
    for name in ('render', 'qa'):
        p = sub.add_parser(name)
        p.add_argument('spec', type=Path)
        p.add_argument('--article', type=Path, required=True)
        p.add_argument('--out', type=Path, required=True)
        if name == 'render':
            p.add_argument('--png', action='store_true', help='PNG is exported by default')
    p = sub.add_parser('demo')
    p.add_argument('case', choices=('article', 'cross-domain', 'families', 'all'), default='all', nargs='?')
    p.add_argument('--out', type=Path, default=ROOT/'runs/demo')
    args = parser.parse_args(argv)
    try:
        if args.command in ('discover', 'collect'):
            if not args.allow_network:
                raise ValueError('Collection uses the network; pass --allow-network only when authorized')
            from asi.corpus import discover, collect
            if args.command == 'discover':
                result = discover(args.sources, args.knowledge, max_urls=args.limit, refresh=args.refresh, progress=False, network=args.network)
            else:
                result = collect(args.knowledge, sources=args.sources, urls=args.url, limit=args.limit, refresh=args.refresh,
                                 include_pdfs=args.include_pdfs, max_linked_pdfs=args.max_linked_pdfs, progress=False, network=args.network)
        elif args.command == 'publication-context':
            if not args.allow_network:
                raise ValueError('Publication reading requires --allow-network')
            from asi.publication import read_publication, coverage
            text, result = read_publication(args.url, network=args.network)
            draft = plan(text, section=args.section)
            result.update(sections=coverage(text), selection=draft['selection'],
                          status='heuristic_draft', semantic_review='pending_host_llm',
                          decisions=[{k: v for k, v in d.items() if k != 'evidence'} for d in draft['decisions']])
            write_json(args.out, result)
        elif args.command == 'audit':
            result = audit(args.knowledge)
        elif args.command == 'search':
            result = search(args.knowledge, args.query, limit=args.limit, family=args.family, relationship=args.relationship,
                            reviewed_only=args.reviewed_only, include_supporting=args.include_supporting, analyses=args.analyses)
        elif args.command in ('show', 'analyze-packet'):
            result = packet(args.knowledge, args.figure_id)
            if args.out:
                write_json(args.out, result)
        elif args.command == 'import-analyses':
            result = import_analyses(args.knowledge, args.source, args.out)
        elif args.command == 'plan':
            result = plan(args.article.read_text(encoding='utf-8'), budget=args.budget, section=args.section,
                          reviewed=json.loads(args.reviewed.read_text(encoding='utf-8')) if args.reviewed else None)
            write_json(args.out, result)
        elif args.command in ('render', 'qa'):
            spec = json.loads(args.spec.read_text(encoding='utf-8'))
            text = args.article.read_text(encoding='utf-8')
            result = render(spec, text, args.knowledge, args.out) if args.command == 'render' else qa(spec, text, args.knowledge, args.out)
        else:
            from asi.examples import demo
            result = demo(args.case, args.knowledge, args.out)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
