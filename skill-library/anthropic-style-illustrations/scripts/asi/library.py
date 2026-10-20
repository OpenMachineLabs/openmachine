"""Streaming corpus retrieval and source-bound analysis validation."""
from collections import Counter
from datetime import date
import fcntl
import os
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
RENDER_FAMILIES = ('flow', 'comparison', 'matrix', 'bar', 'line', 'cover')
ANALYSIS_FAMILIES = RENDER_FAMILIES + ('sequence', 'network', 'concept', 'small_multiples', 'screenshot', 'dumbbell', 'scatter', 'stacked_bar', 'anatomy', 'hierarchy', 'heatmap', 'errorbar')
STOPWORDS = set('a an the and or of to in on for with from by as is are be it its this that these those how what does do can into through using use'.split())


def output_path(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Output must stay inside this Skill directory')
    if path.exists() and path.is_file() and path.stat().st_nlink > 1:
        raise ValueError('Hard-linked output files are not accepted')
    return path


def write_json(path, value):
    path = output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path(path.with_name(path.name + '.tmp'))
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def rows(path):
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('Expected an object')
                    yield value
                except ValueError as exc:
                    raise ValueError(f'{path.name}:{number}: {exc}') from exc


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def safe_url(value):
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Expected a credential-free HTTPS source URL')
    return value


def tokens(text):
    words = re.findall(r'[a-z0-9]+|[^\W\d_]', text.casefold()) if re.search(r'[\u3400-\u9fff]', text) else re.findall(r'[^\W_]+', text.casefold())
    return [w for w in words if w not in STOPWORDS]


def official_url(url):
    return urlsplit(url).hostname in {'www.anthropic.com', 'anthropic.com', 'alignment.anthropic.com', 'transformer-circuits.pub', 'claude.dev', 'claude.com'}


def classification(article):
    if article.get('document_kind') == 'landing_page' or urlsplit(article['url']).path.rstrip('/') in ('', '/research', '/engineering', '/blog', '/articles'):
        return 'landing_page'
    if article.get('source_id') == 'linked_paper':
        provenance = article.get('official_provenance', {})
        if (provenance.get('paper_url') == article['url'] and official_url(provenance.get('article_url', ''))
                and provenance.get('article_content_sha256') and provenance.get('authorship_verified') is True
                and provenance.get('method') == 'official_article_link_and_paper_authors'):
            return 'verified_official_paper'
        return 'supporting_literature'
    return 'source_publication'


def source_hash(article, figure):
    # Bind semantic content and occurrence identity, not refresh timestamps or HTTP chrome.
    fields = ('id', 'article_id', 'asset_id', 'url', 'display_url', 'section', 'caption', 'alt',
              'context_before', 'context_after', 'kind', 'source_locator', 'dimensions')
    return digest(json.dumps({'article': article.get('content_sha256') or article['response_sha256'],
                              'figure': {k: figure[k] for k in fields if k in figure}}, sort_keys=True, ensure_ascii=False))


def lookup(knowledge, figure_id):
    figure = next((x for x in rows(Path(knowledge) / 'figures.jsonl') if x['id'] == figure_id), None)
    if figure is None:
        raise ValueError('Unknown figure: ' + figure_id)
    article = next((x for x in rows(Path(knowledge) / 'articles.jsonl') if x['id'] == figure['article_id']), None)
    if article is None:
        raise ValueError('Figure has no article')
    return article, figure


def validate_analysis(record, article, figure):
    if record.get('figure_id') != figure['id'] or record.get('article_id') != article['id']:
        raise ValueError('Analysis IDs do not match the original occurrence')
    if record.get('source_hash') != source_hash(article, figure):
        raise ValueError('Stale analysis: source hash changed')
    if record.get('family') not in ANALYSIS_FAMILIES:
        raise ValueError('Unknown analysis family')
    for field in ('relationship', 'editorial_purpose', 'information_structure', 'composition', 'palette', 'typography', 'production_hypothesis'):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError('Missing analysis field: ' + field)
    if record.get('production_certainty') != 'uncertain':
        raise ValueError('Production reconstruction must be marked uncertain')
    if not isinstance(record.get('evidence'), dict):
        raise ValueError('Evidence must be an object with separate evidence lists')
    for kind in ('observed', 'source_stated', 'inferred'):
        evidence = record.get('evidence', {}).get(kind)
        if not isinstance(evidence, list) or (kind == 'observed' and not evidence):
            raise ValueError('Evidence must separate observed, source_stated and inferred lists')
        for item in evidence:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('claim', 'locator')):
                raise ValueError('Each evidence item needs a claim and locator')
    inspection = record.get('pixel_inspection', {})
    if not isinstance(inspection, dict):
        raise ValueError('Pixel inspection must be an object')
    if not all(isinstance(inspection.get(k), str) and inspection[k].strip() for k in ('locator', 'date', 'inspector')):
        raise ValueError('Pixel inspection requires locator, date and inspector')
    if inspection.get('method') not in ('official_image_pixels', 'official_pdf_screenshot', 'rendered_page_screenshot', 'live_interaction'):
        raise ValueError('Pixel inspection requires an explicit pixel method; source_markup is not pixel evidence')
    safe_url(article['url'])
    safe_url(inspection['locator'])
    locators = {figure['url'], figure.get('display_url')}
    if inspection['locator'] not in locators:
        raise ValueError('Pixel locator must bind to this figure occurrence')
    if not official_url(article['url']) and classification(article) != 'verified_official_paper':
        raise ValueError('Analysis requires explicit official source provenance')
    if figure.get('source_locator', {}).get('page'):
        if inspection.get('page') != figure['source_locator']['page'] or not inspection.get('region'):
            raise ValueError('PDF inspection needs the exact page and region')
        region = figure['source_locator'].get('region')
        if region and inspection['region'] != region:
            raise ValueError('PDF inspection region does not match the registered occurrence')
    for item in record['evidence']['observed']:
        if item['locator'] != inspection['locator']:
            raise ValueError('Observed evidence must identify the inspected pixel locator')
    source_context = ' '.join(str(v) for v in [article.get('title', ''), article.get('description', ''),
                                             *[figure.get(k, '') for k in ('section', 'caption', 'alt', 'context_before', 'context_after')]])
    for item in record['evidence']['source_stated']:
        if item['locator'] not in (article['url'], article.get('canonical_url'), figure['url']):
            raise ValueError('Source-stated locator must bind to this source')
        if 'quote' in item:
            if not isinstance(item['quote'], str) or not item['quote'] or item['quote'] not in source_context:
                raise ValueError('Source quote is not an exact retained excerpt')
        elif item.get('method') != 'context_review_paraphrase':
            raise ValueError('Source-stated evidence requires a quote or explicit context-review paraphrase')
        else:
            review = item.get('context_review', {})
            if (not isinstance(review, dict) or review.get('source_hash') != record['source_hash'] or review.get('locator') != article['url']
                    or not all(isinstance(review.get(k), str) and review[k].strip() for k in ('reviewer', 'date', 'notes_origin'))):
                raise ValueError('Context-review paraphrase needs source-bound review provenance')
            if date.fromisoformat(review['date']) > date.today():
                raise ValueError('Context review date is in the future')
    for item in record['evidence']['inferred']:
        if not isinstance(item.get('basis'), list) or not item['basis']:
            raise ValueError('Inferred evidence needs observed/source_stated basis references')
        for basis in item['basis']:
            match = re.fullmatch(r'(observed|source_stated):(\d+)', str(basis))
            if not match or int(match[2]) >= len(record['evidence'][match[1]]):
                raise ValueError('Inference basis does not identify retained evidence')
    if date.fromisoformat(inspection['date']) > date.today():
        raise ValueError('Inspection date is in the future')
    for field in ('reusable_rules', 'counterexamples'):
        if not isinstance(record.get(field), list) or not record[field] or not all(isinstance(x, str) and x.strip() for x in record[field]):
            raise ValueError('Missing concrete ' + field)
    transfer = record.get('cross_domain_transfer', {})
    if not isinstance(transfer, dict):
        raise ValueError('Cross-domain transfer must be an object')
    if not all(isinstance(transfer.get(k), str) and transfer[k].strip() for k in ('domain', 'mapping', 'changes', 'limits')):
        raise ValueError('Cross-domain transfer needs domain, mapping, changes and limits')
    return record


def packet(knowledge, figure_id):
    article, figure = lookup(knowledge, figure_id)
    analysis = next((r for r in rows(Path(knowledge)/'analyses.jsonl') if r.get('figure_id') == figure_id), None)
    analysis_status = 'absent'
    if analysis:
        try:
            validate_analysis(analysis, article, figure)
            analysis_status = 'shape_validated_not_endorsed'
        except (ValueError, TypeError, KeyError) as exc:
            analysis_status = 'rejected: ' + str(exc)
            analysis = None
    return {'analysis': analysis, 'analysis_status': analysis_status,
            'figure_id': figure_id, 'article_id': article['id'], 'source_hash': source_hash(article, figure),
            'classification': classification(article), 'article': article, 'figure': figure,
            'image_link': safe_url(figure.get('display_url') or figure['url']),
            'inspection_status': 'retained_pixel_inspection_not_endorsed' if analysis else 'pending_host_multimodal_inspection',
            'instructions': 'Open the source image or located PDF page. Metadata is not pixel evidence. Use the analysis rubric; do not invent inspection.'}


def search(knowledge, query, *, limit=8, family=None, relationship=None, reviewed_only=False, include_supporting=False, analyses=None):
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    knowledge = Path(knowledge)
    articles = {a['id']: a for a in rows(knowledge / 'articles.jsonl')}
    asset_keys = {a['id']: a.get('content_sha256') or a['id'] for a in rows(knowledge / 'assets.jsonl')}
    analysis_by_id = {}
    for a in rows(analyses or knowledge / 'analyses.jsonl'):
        analysis_by_id[a.get('figure_id')] = a
    terms = set(tokens(query))
    if not terms:
        raise ValueError('Search requires words')

    def candidates():
        for figure in rows(knowledge / 'figures.jsonl'):
            article = articles.get(figure['article_id'])
            if not article or (classification(article) in ('supporting_literature', 'landing_page') and not include_supporting):
                continue
            analysis = analysis_by_id.get(figure['id'])
            # Stale critiques stay excluded until an explicit replacement review.
            if analysis and analysis.get('usage') == 'contrast_not_target':
                continue
            if analysis:
                try:
                    validate_analysis(analysis, article, figure)
                except (ValueError, TypeError, KeyError):
                    analysis = None
            if reviewed_only and analysis is None:
                continue
            # Metadata families are only lexical hints; never visual conclusions.
            text = ' '.join(str(figure.get(k, '')) for k in ('caption', 'alt', 'section', 'context_before', 'context_after'))
            text += ' ' + article.get('title', '') + ' ' + article.get('description', '')
            inferred_family = next((f for f, words in [('flow', ('workflow', 'flow', 'orchestrator')), ('comparison', ('comparison', 'versus')), ('matrix', ('matrix',)), ('bar', ('bar chart',)), ('line', ('line chart',)), ('cover', ('illustration',))] if any(w in text.lower() for w in words)), None)
            chosen_family = analysis['family'] if analysis else inferred_family
            relation = analysis['relationship'] if analysis else text
            if family and chosen_family != family:
                continue
            if relationship and relationship.casefold() not in relation.casefold():
                continue
            if analysis:
                text += ' ' + ' '.join(str(analysis.get(k, '')) for k in ('title', 'relationship', 'editorial_purpose', 'information_structure'))
            yield figure, article, analysis, tokens(text), chosen_family

    n = total = 0
    df = Counter()
    for _, _, _, words, _ in candidates():
        n += 1
        total += len(words)
        df.update(terms.intersection(words))
    average = total / max(1, n)
    best = []
    for figure, article, analysis, words, chosen_family in candidates():
        counts = Counter(words)
        score = sum(math.log(1 + (n - df[t] + .5) / (df[t] + .5)) * counts[t] * 2.2 /
                    (counts[t] + 1.2 * (.25 + .75 * len(words) / max(1, average))) for t in terms if counts[t])
        if not score:
            continue
        result = {'figure_id': figure['id'], 'article_id': article['id'], 'asset_id': figure['asset_id'],
                  'source_hash': source_hash(article, figure), 'source_url': article['url'], 'image_link': figure.get('display_url') or figure['url'],
                  'title': article['title'], 'family': chosen_family, 'classification': classification(article),
                  'score': round(score, 6), 'evidence_level': 'validated_analysis' if analysis else 'metadata_only',
                  'review_status': 'import_shape_validated_not_endorsed' if analysis else 'not_inspected', 'analysis': analysis}
        # Rank relevance first; review is evidence quality, not a relevance override.
        if analysis:
            structure = set(tokens(analysis['relationship']))
            score += 2 * len(terms & structure) / len(terms)
        result['score'] = round(score, 6)
        best.append((score, bool(analysis), figure['id'], result))
    seen = set()
    results = []
    for _, _, _, result in sorted(best, reverse=True):
        asset_key = asset_keys.get(result['asset_id'], result['asset_id'])
        if asset_key not in seen:
            seen.add(asset_key)
            results.append(result)
            if len(results) == limit:
                break
    return results


def audit(knowledge):
    knowledge = Path(knowledge)
    tables = {name: list(rows(knowledge / (name + '.jsonl'))) for name in ('articles', 'assets', 'figures', 'analyses')}
    duplicates = {}
    for name, records in tables.items():
        key = 'figure_id' if name == 'analyses' else 'id'
        duplicates[name] = sorted(k for k, n in Counter(r.get(key, '') for r in records).items() if n > 1)
    duplicates['case_ids'] = sorted(k for k, n in Counter(r['case_id'] for r in tables['analyses'] if r.get('case_id')).items() if n > 1)
    articles = {a['id']: a for a in tables['articles']}
    assets = {a['id'] for a in tables['assets']}
    figures = {f['id']: f for f in tables['figures']}
    invalid, valid, stale = [], [], 0
    for a in tables['analyses']:
        try:
            f = figures[a['figure_id']]
            validate_analysis(a, articles[f['article_id']], f)
            valid.append(a)
        except (ValueError, KeyError, TypeError) as exc:
            stale += str(exc).startswith('Stale analysis')
            invalid.append({'figure_id': a.get('figure_id'), 'error': str(exc)})
    errors = ['No ' + name + ' records' for name, records in tables.items() if not records]
    return {'documents': len(articles), 'figures': len(figures), 'assets': len(assets),
            'supporting_literature': sum(classification(a) == 'supporting_literature' for a in articles.values()),
            'classifications': dict(Counter(classification(a) for a in articles.values())),
            'broken_references': [f['id'] for f in figures.values() if f['article_id'] not in articles or f['asset_id'] not in assets],
            'analysis_count': len(tables['analyses']), 'valid_analyses': len(valid), 'stale_analyses': stale,
            'invalid_or_stale_analyses': invalid, 'duplicate_ids': duplicates, 'errors': errors,
            'visual_review': {'retained_inspection_records': len(valid), 'independently_endorsed': False},
            'reviewed_sources': dict(Counter(articles[a['article_id']]['url'] for a in valid)),
            'reviewed_families': dict(Counter(a['family'] for a in valid)),
            'negative_contrasts': sum(a.get('usage') == 'contrast_not_target' for a in valid),
            'open_failures': sum(1 for _ in rows(knowledge / 'failures.jsonl'))}


def import_analyses(knowledge, source, destination):
    destination = output_path(destination)
    incoming = list(rows(source))
    if not incoming:
        raise ValueError('No analyses to import')
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path(destination.with_name(destination.name + '.lock'))
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = list(rows(destination))
        for records in (incoming, existing):
            for key in ('figure_id', 'case_id'):
                values = [r[key] for r in records if key in r]
                if len(values) != len(set(values)):
                    raise ValueError('Duplicate analysis ' + key)
        merged = {r['figure_id']: r for r in existing}
        changed = 0
        for record in incoming:
            article, figure = lookup(knowledge, record.get('figure_id', ''))
            validate_analysis(record, article, figure)
            previous = merged.get(figure['id'])
            current = {k: v for k, v in record.items() if k != 'replacement_history'}
            if previous and current == {k: v for k, v in previous.items() if k != 'replacement_history'}:
                continue
            if previous:
                current['replacement_history'] = [*previous.get('replacement_history', []),
                                                   {k: v for k, v in previous.items() if k != 'replacement_history'}]
            merged[figure['id']] = current
            changed += 1
        cases = [r['case_id'] for r in merged.values() if r.get('case_id')]
        if len(cases) != len(set(cases)):
            raise ValueError('Duplicate analysis case_id across destination')
        if changed:
            tmp = output_path(destination.with_name(destination.name + '.tmp'))
            with tmp.open('w', encoding='utf-8') as stream:
                for record in merged.values():
                    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return {'validated': len(incoming), 'changed': changed, 'total': len(merged), 'endorsement': False, 'output': str(destination)}
