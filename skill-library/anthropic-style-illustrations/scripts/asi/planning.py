"""Whole-document draft planning and explicit, source-bound semantic review."""
import re
from .library import RENDER_FAMILIES, digest


def sections(text):
    result = []
    current = {'id': 'section-1', 'heading': 'Preamble', 'level': 0, 'start_line': 1, 'lines': []}
    fence = None
    source_lines = text.splitlines()
    for number, line in enumerate(source_lines, 1):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            current['lines'].append(line)
            continue
        heading = re.match(r'^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$', line) if fence is None else None
        setext = re.match(r'^\s{0,3}(=+|-+)\s*$', line) if fence is None else None
        if setext and current['lines'] and current['lines'][-1].strip():
            title = current['lines'].pop()
            if current['lines'] or current['level']:
                result.append(current)
            current = {'id': f'section-{len(result)+1}', 'heading': title.strip(), 'level': 1 if setext[1][0] == '=' else 2, 'start_line': number - 1, 'lines': [title, line]}
        elif heading:
            if current['lines'] or current['level']:
                result.append(current)
            current = {'id': f'section-{len(result)+1}', 'heading': heading[2], 'level': len(heading[1]), 'start_line': number, 'lines': [line]}
        else:
            current['lines'].append(line)
    if current['lines'] or current['level']:
        result.append(current)
    for index, item in enumerate(result):
        item['text'] = '\n'.join(item.pop('lines'))
        item['end_line'] = result[index+1]['start_line'] - 1 if index+1 < len(result) else len(source_lines)
        item['anchor'] = item['id']
    return result


def plan(text, *, budget=3, section=None, reviewed=None):
    if not isinstance(budget, int) or not 0 <= budget <= 20:
        raise ValueError('Illustration budget must be between 0 and 20')
    parts = sections(text)
    if not parts:
        raise ValueError('Input article is empty')
    selected = [p['id'] for p in parts if section is None or section in (p['id'], p['heading'])]
    if section is not None and not selected:
        raise ValueError('Section not found')
    result = {'schema_version': 1, 'input_hash': digest(text), 'full_context': text, 'sections': parts,
              'budget': budget, 'selection': selected, 'status': 'heuristic_draft',
              'semantic_review': 'pending_host_llm', 'decisions': []}
    if reviewed is not None:
        if reviewed.get('input_hash') != result['input_hash'] or reviewed.get('full_context') != text:
            raise ValueError('Reviewed plan does not match the complete input')
        review = reviewed.get('review', {})
        if not all(isinstance(review.get(k), str) and review[k].strip() for k in ('reviewer', 'date', 'rationale')):
            raise ValueError('Reviewed plan needs an explicit review receipt')
        decisions = reviewed.get('decisions', [])
        if sorted(d.get('section_id', '') for d in decisions) != sorted(p['id'] for p in parts):
            raise ValueError('Reviewed plan must decide every section exactly once')
        for d in decisions:
            if d.get('decision') not in ('illustrate', 'no_image') or not d.get('reason'):
                raise ValueError('Every section needs a decision and reason')
            p = next(p for p in parts if p['id'] == d['section_id'])
            for anchor in d.get('evidence', []):
                if not isinstance(anchor, str) or not anchor or anchor not in p['text']:
                    raise ValueError('Plan evidence is absent from its section')
            if d['decision'] == 'illustrate' and (not d.get('evidence') or d.get('family') not in RENDER_FAMILIES):
                raise ValueError('Illustration needs evidence and a supported family')
        if sum(d['decision'] == 'illustrate' and d['section_id'] in selected for d in decisions) > budget:
            raise ValueError('Reviewed plan exceeds budget')
        result.update(decisions=decisions, status='reviewed_semantic_plan', semantic_review=review)
        return result
    used = 0
    for p in parts:
        body = p['text'].casefold()
        family = next((name for name, hints in [('comparison', ('versus', 'compare', 'comparison', '\u5bf9\u6bd4')), ('flow', ('workflow', 'step', 'sequence', '\u6d41\u7a0b')), ('matrix', ('criteria', 'matrix', '\u77e9\u9635'))] if any(h in body for h in hints)), None)
        chosen = p['id'] in selected and family is not None and used < budget
        evidence = next((line.strip() for line in p['text'].splitlines() if line.strip() and not line.startswith('#')), p['heading'])
        result['decisions'].append({'section_id': p['id'], 'decision': 'illustrate' if chosen else 'no_image',
                                    'family': family if chosen else None, 'evidence': [evidence] if chosen else [],
                                    'reason': 'Draft relationship cue; host must review meaning.' if chosen else 'Outside selection, no clear relationship cue, or budget reserved; keep prose.',
                                    'reader_question': 'How do these elements relate?' if chosen else None})
        used += chosen
    return result


def validate_spec(spec, text, knowledge):
    from .library import lookup, source_hash, safe_url
    if spec.get('input_hash') != digest(text):
        raise ValueError('Spec input hash does not match article')
    parts = {p['id']: p for p in sections(text)}
    claims = spec.get('claims', [])
    if not claims:
        raise ValueError('Spec needs input-bound claims')
    ids = [c.get('id') for c in claims]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Claim IDs must be unique and nonempty')
    for claim in claims:
        source = parts.get(claim.get('section_id'))
        if not source or not claim.get('quote') or claim['quote'] not in source['text'] or not claim.get('text'):
            raise ValueError('Claim requires an exact input evidence anchor')
    refs = spec.get('references', [])
    if len({r.get('figure_id') for r in refs}) != len(refs):
        raise ValueError('Duplicate reference IDs')
    for ref in refs:
        article, figure = lookup(knowledge, ref.get('figure_id'))
        if ref.get('source_hash') != source_hash(article, figure) or ref.get('article_id') != article['id']:
            raise ValueError('Reference identity or hash is stale')
        if ref.get('source_url') != article['url']:
            raise ValueError('Reference URL does not match source')
        safe_url(ref['source_url'])
        if not ref.get('transfer') or ref.get('evidence_level') not in ('metadata_only', 'validated_analysis'):
            raise ValueError('Reference needs transfer scope and evidence level')
        if ref['evidence_level'] == 'validated_analysis':
            from .library import validate_analysis
            validate_analysis(ref.get('analysis', {}), article, figure)
    if spec.get('family') not in RENDER_FAMILIES:
        raise ValueError('Unsupported figure family')
    for key in ('title', 'caption', 'alt'):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise ValueError('Missing figure ' + key)
    for item in spec.get('nodes', []) + spec.get('items', []) + spec.get('data', []):
        if not item.get('claim_ids') or any(c not in ids for c in item['claim_ids']):
            raise ValueError('Every visual item must bind to input claim IDs')
    if spec.get('semantic_review'):
        if not all(spec['semantic_review'].get(k) for k in ('reviewer', 'date', 'rationale')):
            raise ValueError('Incomplete semantic review receipt')
    return spec
