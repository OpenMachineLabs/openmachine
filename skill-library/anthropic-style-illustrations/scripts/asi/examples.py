"""Executable original examples grounded in the retained corpus, never fake reviews."""
from pathlib import Path
from .library import ROOT, audit, digest, output_path, packet, search, write_json
from .planning import plan, sections
from .rendering import render, qa


def base_spec(text, title, family, section_id, quote):
    return {'schema_version': 1, 'input_hash': digest(text), 'title': title, 'family': family,
            'subtitle': 'Original explanatory composition', 'caption': 'Draft composition. Reference metadata is not visual inspection.',
            'alt': title, 'claims': [{'id': 'c1', 'section_id': section_id, 'quote': quote, 'text': quote}], 'references': []}


def reference(result):
    return {key: result[key] for key in ('figure_id', 'article_id', 'source_hash', 'source_url', 'evidence_level', 'analysis')} | {
        'transfer': ('Reuse the retained source reviewer relationship study; the composition is original and no new pixel review is claimed.' if result['analysis'] else 'Use the named relationship as a metadata lead only; no inspected appearance is claimed.')}


def demo(case, knowledge, out):
    out = output_path(out)
    cases = ('article', 'cross-domain', 'families') if case == 'all' else (case,)
    reports = {}
    for name in cases:
        destination = output_path(out/name)
        destination.mkdir(parents=True, exist_ok=True)
        if name == 'families':
            reports[name] = family_examples(knowledge, destination)
            continue
        text = (ROOT/'examples'/name/'article.md').read_text(encoding='utf-8')
        output_path(destination/'article.md').write_text(text, encoding='utf-8')
        planned = plan(text, budget=1, section="section-3" if name == "article" else "section-2")
        write_json(destination/'plan.json', planned)
        query = 'routing parallelization workflow' if name == 'article' else 'orchestrator workers workflow'
        results = search(knowledge, query, limit=3)
        if not results:
            raise ValueError('Demo requires real corpus retrieval results')
        write_json(destination/'retrieval.json', results)
        write_json(destination/'analysis-packet.json', packet(knowledge, results[0]['figure_id']))
        if name == 'article':
            quote = 'Routing invites a question about selection. Parallelization invites a question\nabout concurrent work.'
            spec = base_spec(text, 'Two questions before choosing a workflow', 'comparison', 'section-3', quote)
            spec.update(columns=['Routing • Selection', 'Parallelization • Concurrency'],
                        items=[{'id': 'questions', 'cells': ['Which path fits this input?', 'Which work can proceed concurrently?'], 'claim_ids': ['c1']},
                               {'id': 'boundaries', 'cells': ['Read the routing section for conditions.', 'Read the parallelization section for conditions.'], 'claim_ids': ['c1']}],
                        caption='An original reading map based on workflow names. The source article supplies the actual usage conditions; this comparison does not rank performance.')
        else:
            quote = sections(text)[1]['text'].split('\n', 1)[1].strip()
            spec = base_spec(text, 'Separate reviews, one coordinated decision', 'flow', 'section-2', quote)
            spec['nodes'] = [{'id': id_, 'label': label, 'row': row, 'column': col, 'claim_ids': ['c1']} for id_, label, row, col in [
                ('intake', 'Intake • Item description', 0, 0), ('safety', 'Safety reviewer • Safety note', 0, 1),
                ('parts', 'Parts reviewer • Parts note', 1, 1), ('join', 'Coordinator • Both notes', 0, 2)]]
            spec['edges'] = [{'from': a, 'to': b, 'meaning': meaning, 'claim_ids': ['c1']} for a,b,meaning in [
                ('intake','safety','request review'), ('intake','parts','request review'), ('safety','join','return safety note'),
                ('parts','join','return parts note'), ('join','intake','request clarification')]]
            spec['caption'] = 'A proposed human workflow: both reviews inform coordination, and incomplete requests return to intake. Arrows encode information handoffs; no timing or success rate is implied.'
        spec['references'] = [reference(results[0])]
        report = render(spec, text, knowledge, destination)
        qa(spec, text, knowledge, destination)
        reports[name] = report
    write_json(out/'demo-report.json', reports)
    return reports


def family_examples(knowledge, destination):
    counts = audit(knowledge)
    supporting = counts['supporting_literature']
    other = counts['classifications'].get('source_publication', 0) + counts['classifications'].get('verified_official_paper', 0)
    landing = counts['classifications'].get('landing_page', 0)
    text = (f'# Corpus coverage\n\nThese are counts derived from the retained local indexes, not model performance measurements.\n\n'
            f'## Counts\n\nSource publications: {other} documents. Supporting literature: {supporting} documents. Landing pages: {landing} documents.\n\n'
            '## Reading policy\n\nDefault retrieval includes source publications. Supporting literature requires explicit inclusion. '
            'Metadata retrieval leads to source inspection, then analysis import.\n')
    output_path(destination/'article.md').write_text(text, encoding='utf-8')
    write_json(destination/'data-provenance.json', counts)
    write_json(destination/'plan.json', plan(text, budget=2))
    results = search(knowledge, 'comparison workflow', limit=2)
    write_json(destination/'retrieval.json', results)
    quote = sections(text)[1]['text'].split('\n', 1)[1].strip()
    bar = base_spec(text, 'What the local corpus contains', 'bar', 'section-2', quote)
    bar.update(unit='documents', data=[{'id': 'source', 'label': 'Source publications', 'value': other, 'evidence': f'Source publications: {other} documents.', 'claim_ids':['c1']},
                                       {'id': 'support', 'label': 'Supporting literature', 'value': supporting, 'evidence': f'Supporting literature: {supporting} documents.', 'claim_ids':['c1']},
                                       {'id': 'landing', 'label': 'Landing pages', 'value': landing, 'evidence': f'Landing pages: {landing} documents.', 'claim_ids':['c1']}],
               caption='Counts computed from the retained articles index. Supporting literature and landing pages are excluded from default style retrieval.')
    policy_quote = sections(text)[2]['text'].split('\n', 1)[1].strip()
    matrix = base_spec(text, 'Evidence determines the next action', 'matrix', 'section-3', policy_quote)
    matrix.update(columns=['Record class', 'Default retrieval', 'Next action'], items=[
        {'id':'source', 'cells':['Source publication', 'Included', 'Inspect source pixels'], 'claim_ids':['c1']},
        {'id':'support', 'cells':['Supporting literature', 'Explicit opt-in', 'Check authorship and relevance'], 'claim_ids':['c1']},
        {'id':'landing', 'cells':['Landing page', 'Explicit opt-in', 'Locate the actual publication'], 'claim_ids':['c1']}])
    cover = base_spec(text, 'From a reference to an original explanation', 'cover', 'section-3', policy_quote)
    cover['items'] = [{'id': i, 'label': label, 'claim_ids':['c1']} for i,label in [('retrieve','Retrieve a source-bound reference'),('inspect','Inspect before describing appearance'),('create','Create an original explanation')]]
    specs = {'bar': (bar,text), 'matrix': (matrix,text), 'cover': (cover,text)}
    # A line demonstrates real cumulative index counts, not fabricated measurements.
    from .library import rows
    article_id = 'article_ffc229d83891918f82064b92'
    occurrences = sorted([r for r in rows(Path(knowledge)/'figures.jsonl') if r['article_id']==article_id], key=lambda r:r['position'])
    if len(occurrences) < 3:
        raise ValueError('Line example requires retained Building effective agents occurrences')
    selected = [occurrences[0], occurrences[len(occurrences)//2], occurrences[-1]]
    data = []
    facts = []
    for i, row in enumerate(selected):
        count = sum(r['position'] <= row['position'] for r in occurrences)
        label = f'Checkpoint {i+1}'
        facts.append(f'{label}: at {row["position"]} ordinal, cumulative count is {count} occurrences.')
        data.append({'id':f'p{i}', 'label':label, 'x':row['position'], 'value':count, 'evidence':facts[-1], 'claim_ids':['c1']})
    line_text = '# Cumulative figure occurrences\n\n'+'\n'.join(facts)+'\n\nCounts are computed from the retained Building effective agents figure positions. This is an index traversal, not a time series or model benchmark.\n'
    line = base_spec(line_text, 'An index traversal, not a performance curve', 'line', 'section-1', '\n'.join(facts))
    line.update(data=data, unit='occurrences', x_unit='ordinal', caption='Cumulative retained figure occurrences by source position. Points are derived from local metadata; the connecting line only guides reading.')
    write_json(destination/'line-provenance.json', [{'figure_id':r['id'], 'position':r['position']} for r in occurrences])
    specs['line'] = line,line_text
    reports = {}
    for family, (spec, article_text) in specs.items():
        spec['references'] = [reference(results[0])] if results else []
        target = output_path(destination/family)
        target.mkdir(parents=True, exist_ok=True)
        output_path(target/'article.md').write_text(article_text, encoding='utf-8')
        reports[family] = render(spec, article_text, knowledge, target)
        qa(spec, article_text, knowledge, target)
    return reports
