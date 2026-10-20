#!/usr/bin/env python3
"""Offline, article-bound illustration drafts. QA checks evidence, not meaning."""

import argparse
import hashlib
import html
import json
import math
import re
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'references/source-index.json'
FAMILIES = ('flow', 'comparison', 'matrix', 'bar', 'line', 'cover')
COLORS = ('#dce9e0', '#e5e0ef', '#f3e4cc', '#dce8ee')
INK = '#243b37'
BACKGROUND = '#fcfaf5'
SOURCE_HOSTS = {'www.anthropic.com', 'www-cdn.anthropic.com',
                'alignment.anthropic.com', 'claude.dev', 'arxiv.org'}
ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')
NUMBER = r'[-+]?(?:\d+(?:\.\d+)?|\.\d+)'


def read_article(path):
    """Decode without newline normalization: the SHA covers the complete file."""
    raw = Path(path).read_bytes()
    return raw.decode('utf-8'), hashlib.sha256(raw).hexdigest()


def read_json(path):
    """Reject ambiguous duplicate keys as well as nonstandard numeric constants."""
    def invalid(value):
        raise ValueError(f'Nonfinite JSON number: {value}')

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result

    return json.loads(
        Path(path).read_text(encoding='utf-8'),
        parse_constant=invalid,
        object_pairs_hook=unique_keys,
    )


def safe_output(path):
    """Check every output, including existing child symlinks, before writing.

    This is a local CLI boundary, not a defense against concurrent filesystem
    replacement by another process. Never run it in an untrusted shared folder.
    """
    path = Path(path).absolute()
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or resolved == ROOT:
        raise ValueError(f'Output must be inside Skill: {path}')
    for part in (path, *path.parents):
        if part == ROOT:
            break
        if part.is_symlink():
            raise ValueError(f'Output symlink is not allowed: {part}')
    if path.exists() and path.is_file() and path.stat().st_nlink > 1:
        raise ValueError(f'Output hard link is not allowed: {path}')
    return path


def write_text(path, value):
    path = safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def valid_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    parts = urlsplit(value)
    return parts.scheme in ('http', 'https') and bool(parts.hostname) and not parts.username


def valid_source_url(url):
    return valid_url(url) and urlsplit(url).scheme == 'https' and urlsplit(url).hostname in SOURCE_HOSTS


def load_index():
    """Validate the existing curated records; never fetch source media."""
    data = read_json(INDEX)
    records = data.get('records', [])
    if data.get('version') != 1 or len(records) != 23:
        raise ValueError('Expected version 1 index with 23 curated cases')
    seen = set()
    required = ('family', 'relationship', 'inference', 'rule', 'limit')
    for record in records:
        case_id = record.get('case_id')
        if not isinstance(case_id, str) or not ID.fullmatch(case_id) or case_id in seen:
            raise ValueError('Invalid or duplicate case ID')
        seen.add(case_id)
        for field in required:
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError(f'{case_id}: missing {field}')
        if not all(valid_source_url(record.get(key)) for key in ('article_url', 'image_url')):
            raise ValueError(f'{case_id}: invalid source links')
        if not record.get('observed') or not all(isinstance(x, str) for x in record['observed']):
            raise ValueError(f'{case_id}: missing observations')
        if not isinstance(record.get('provenance'), dict):
            raise ValueError(f'{case_id}: missing provenance')
        if record.get('usage') not in ('reference', 'contrast_not_target'):
            raise ValueError(f'{case_id}: invalid usage')
    if len({r['article_url'] for r in records}) != 14:
        raise ValueError('Expected 14 source articles')
    if not any(r['case_id'] == 'E8' and r['usage'] == 'contrast_not_target' for r in records):
        raise ValueError('E8 must remain a negative contrast case')
    return records


def search(query, family=None, include_contrast=False):
    tokens = re.findall(r'\w+', query.casefold())
    matches = []
    for record in load_index():
        if record['usage'] != 'reference' and not include_contrast:
            continue
        if family and record['family'] != family:
            continue
        corpus = ' '.join(str(record[k]) for k in (
            'case_id', 'family', 'relationship', 'observed', 'inference', 'rule', 'limit'
        )).casefold()
        score = sum(token in corpus for token in tokens)
        if score or not tokens:
            matches.append(dict(record, score=score))
    return sorted(matches, key=lambda row: (-row['score'], row['case_id']))


def sections(article):
    """ATX/setext sections; fenced code never creates headings or draft claims."""
    result = []
    title, body, start = 'Preamble', [], 1
    fence = None
    lines = article.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            body.append(line)
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
            i += 1
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            body.append(line)
            i += 1
            continue
        heading = re.match(r'^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$', line)
        setext = (i + 1 < len(lines) and line.strip()
                  and re.fullmatch(r' {0,3}(?:=+|-+)\s*', lines[i + 1]))
        if heading or setext:
            if body or title != 'Preamble':
                result.append({'title': title, 'body': '\n'.join(body), 'line': start})
            title = heading[1] if heading else line.strip()
            start, body = i + 1, []
            i += 1 if heading else 2
        else:
            body.append(line)
            i += 1
    if body or title != 'Preamble':
        result.append({'title': title, 'body': '\n'.join(body), 'line': start})
    return result


def prose_lines(body):
    fence = None
    for line in body.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            continue
        text = line.strip()
        if text and not text.startswith(('<!--', '|', '>')):
            yield text


def plan(article, digest, budget=3):
    if budget < 0:
        raise ValueError('Budget must be nonnegative')
    rows = []
    used = 0
    for section in sections(article):
        prose = list(prose_lines(section['body']))
        eligible = sum(len(line) for line in prose) >= 80
        selected = eligible and used < budget
        used += selected
        rows.append(dict(section, decision='image' if selected else 'no-image',
                         reason='Heuristic: substantial prose within budget' if selected
                         else 'Heuristic: short/code-only section or budget exhausted'))
    return {'article_sha256': digest, 'budget': budget, 'sections': rows,
            'approval': 'heuristic only; no semantic or visual approval'}


def draft(article, digest):
    """Use prose verbatim; do not infer causal edges from section order."""
    candidates = [line for section in sections(article)
                  for line in prose_lines(section['body']) if len(line) <= 600]
    if not candidates:
        raise ValueError('No suitable prose for a draft; author a spec manually')
    selected = candidates[:4]
    return {
        'version': 1, 'article_sha256': digest, 'family': 'comparison',
        'title': 'Selected article excerpts',
        'nodes': [{'id': f'n{i}', 'label': quote, 'quote': quote}
                  for i, quote in enumerate(selected, 1)],
        'edges': [], 'case_refs': [{'case_id': 'C1',
                                  'article_url': next(r['article_url'] for r in load_index()
                                                      if r['case_id'] == 'C1')}],
        'citations': [],
        'draft_note': 'Original concept draft. Peer cards imply no sequence or measured difference.'
    }


def xml_text(value):
    """XML 1.0 and HTML share escaped text; reject forbidden control characters."""
    value = str(value)
    if any(not (c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
           for c in value):
        raise ValueError('Text contains an invalid XML character')
    return html.escape(value, quote=True)


def qa(article, digest, spec):
    """Structural/evidence QA deliberately does not assert semantic entailment."""
    errors = []
    if not isinstance(spec, dict):
        return {'ok': False, 'errors': ['Spec must be an object']}
    if spec.get('version') != 1:
        errors.append('Spec version must be 1')
    if spec.get('article_sha256') != digest:
        errors.append('SHA mismatch: spec must bind the complete article bytes')
    family = spec.get('family')
    if family not in FAMILIES:
        errors.append('Unknown family')

    def text(value, where, limit=600):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            errors.append(f'{where}: expected nonempty text up to {limit} characters')
            return False
        try:
            xml_text(value)
        except ValueError as exc:
            errors.append(f'{where}: {exc}')
            return False
        return True

    def anchor(item, where):
        quote = item.get('quote')
        if text(quote, f'{where}.quote', 4000) and quote not in article:
            errors.append(f'{where}: exact quote missing from article')

    text(spec.get('title'), 'title', 160)
    nodes = spec.get('nodes', [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 16:
        errors.append('Expected 1–16 nodes')
        nodes = []
    ids = set()
    node_map = {}
    for node in nodes:
        if not isinstance(node, dict):
            errors.append('Node must be an object')
            continue
        nid = node.get('id')
        if not isinstance(nid, str) or not ID.fullmatch(nid) or nid in ids:
            errors.append('Invalid or duplicate node ID')
        else:
            ids.add(nid)
            node_map[nid] = node
        text(node.get('label'), 'node.label')
        anchor(node, f'node {nid}')
    edges = spec.get('edges', [])
    if not isinstance(edges, list) or len(edges) > 32:
        errors.append('Expected at most 32 edges')
        edges = []
    edge_ids = set()
    for edge in edges:
        if not isinstance(edge, dict):
            errors.append('Edge must be an object')
            continue
        eid = edge.get('id')
        if not isinstance(eid, str) or not ID.fullmatch(eid) or eid in edge_ids or eid in ids:
            errors.append('Invalid or duplicate edge ID')
        else:
            edge_ids.add(eid)
        if edge.get('from') not in ids or edge.get('to') not in ids:
            errors.append('Edge endpoints must name existing nodes')
        text(edge.get('label'), 'edge.label', 120)
        anchor(edge, f'edge {eid}')
    if family != 'flow' and edges:
        errors.append('Edges are only rendered in the flow family')
    records = {row['case_id']: row for row in load_index()}
    refs = spec.get('case_refs')
    if not isinstance(refs, list) or not refs:
        errors.append('At least one linked case reference is required')
    else:
        for ref in refs:
            record = records.get(ref.get('case_id')) if isinstance(ref, dict) else None
            if not record or ref.get('article_url') != record['article_url']:
                errors.append('Invalid linked case reference')
            elif record['usage'] != 'reference':
                errors.append('Negative contrast case cannot be a design target')
    citations = spec.get('citations', [])
    if not isinstance(citations, list) or len(citations) > 32:
        errors.append('Expected at most 32 citations')
    else:
        for citation in citations:
            if not isinstance(citation, dict):
                errors.append('Citation must be an object')
                continue
            text(citation.get('label'), 'citation.label', 160)
            url = citation.get('url')
            if not valid_url(url) or url not in article:
                errors.append('Citation URL must occur in article and use HTTP(S)')
            anchor(citation, 'citation')
    if family == 'matrix':
        columns = spec.get('columns')
        if not isinstance(columns, list) or not 1 <= len(columns) <= 4:
            errors.append('Matrix requires 1–4 columns')
        else:
            for column in columns:
                text(column, 'column', 80)
            for node in node_map.values():
                cells = node.get('cells')
                if not isinstance(cells, list) or len(cells) != len(columns):
                    errors.append('Matrix cells must match columns')
                    continue
                for cell in cells:
                    if not isinstance(cell, dict):
                        errors.append('Cell must contain text and quote')
                        continue
                    text(cell.get('text'), 'cell.text', 240)
                    anchor(cell, 'cell')
    if family in ('bar', 'line'):
        unit = spec.get('unit')
        unit_ok = text(unit, 'unit', 40) and bool(re.search(r'[^\d\s.,+-]', unit))
        if not unit_ok:
            errors.append('Charts require an explicit unit')
        if type(spec.get('baseline')) not in (int, float) or spec['baseline'] != 0:
            errors.append('Charts require a zero baseline')
        for node in node_map.values():
            value = node.get('value')
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                errors.append('Chart values must be finite nonnegative numbers')
                continue
            quote = node.get('quote', '')
            if unit_ok and isinstance(quote, str):
                pattern = r'(?<![\w.,+−-])(' + NUMBER + r')\s*' + re.escape(unit) + r'(?!\w)'
                matches = re.findall(pattern, quote)
                if not any(float(match) == value for match in matches):
                    errors.append('Chart value with unit must occur exactly in its claim quote')
        if family == 'line' and len(node_map) < 2:
            errors.append('Line charts require at least two ordered points')
    # JSON can also contain nonfinite values in unused metadata.
    try:
        json.dumps(spec, allow_nan=False)
    except (ValueError, TypeError):
        errors.append('Spec must be finite JSON data')
    return {'ok': not errors, 'errors': errors, 'article_sha256': digest,
            'semantic_approval': False, 'visual_approval': False,
            'limitations': ['Exact quotes do not prove that labels or relationships are entailed.',
                            'Inspect PNG and review source meaning before publication.']}


def wrapped(value, width):
    """Bound text by code points; final font metrics still need visual inspection."""
    return [line for paragraph in str(value).splitlines() or ['']
            for line in (textwrap.wrap(paragraph, width, break_long_words=True,
                                       break_on_hyphens=False) or [''])]


def svg_text(value, x, y, width=45, size=18, color=INK):
    lines = wrapped(value, width)
    parts = [f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
             'font-family="sans-serif">']
    for i, line in enumerate(lines):
        parts.append(f'<tspan x="{x}" dy="{0 if i == 0 else size * 1.4}">'
                     f'{xml_text(line)}</tspan>')
    parts.append('</text>')
    return ''.join(parts)


def rect(x, y, width, height, fill, radius=12):
    return (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'rx="{radius}" fill="{fill}" stroke="#9baaa2"/>')


def render_cards(spec, top):
    """Peer comparisons and cover excerpts keep equal visual weight."""
    parts = []
    nodes = spec['nodes']
    columns = 1 if spec['family'] == 'cover' else 2
    width = 1080 / columns - 20
    y = top
    for offset in range(0, len(nodes), columns):
        row = nodes[offset:offset + columns]
        wrap_width = int((width - 40) / 10)
        height = max(100, max(len(wrapped(n['label'], wrap_width)) for n in row) * 26 + 48)
        for j, node in enumerate(row):
            x = 60 + j * (width + 20)
            parts.append(rect(x, y, width, height, COLORS[(offset + j) % len(COLORS)]))
            parts.append(svg_text(node['label'], x + 20, y + 32, wrap_width))
        y += height + 24
    return parts, y


def render_flow(spec, top):
    """Lay out a readable main chain, with side lanes for authored branches.

    Node order comes from the spec, never from inference about article order.
    An adjacent forward edge uses the inter-card gap. Other edges retain their
    direction in separate side lanes; dense graphs still require visual review.
    """
    parts, boxes = [], {}
    nodes = spec['nodes']
    edges = spec.get('edges', [])
    order = {node['id']: i for i, node in enumerate(nodes)}
    direct = set()
    occupied = set()
    for edge in edges:
        pair = (edge['from'], edge['to'])
        if order[edge['to']] == order[edge['from']] + 1 and pair not in occupied:
            direct.add(edge['id'])
            occupied.add(pair)
    side_edges = [edge for edge in edges if edge['id'] not in direct]
    card_width = 660 if side_edges else 940
    x = 60 if side_edges else 130
    y = top
    for i, node in enumerate(nodes):
        wrap_width = int((card_width - 40) / 10)
        height = max(96, len(wrapped(node['label'], wrap_width)) * 26 + 48)
        parts.append(rect(x, y, card_width, height, COLORS[i % len(COLORS)]))
        parts.append(svg_text(node['label'], x + 20, y + 34, wrap_width))
        boxes[node['id']] = (y, height)
        y += height + 110
    for edge in edges:
        start, start_height = boxes[edge['from']]
        end, end_height = boxes[edge['to']]
        if edge['id'] in direct:
            center = x + card_width / 2
            parts.append(f'<path d="M {center} {start + start_height} V {end - 4}" '
                         f'fill="none" stroke="{INK}" stroke-width="2"/>')
            parts.append(f'<polygon points="{center},{end - 4} {center - 6},{end - 15} '
                         f'{center + 6},{end - 15}" fill="{INK}"/>')
            parts.append(svg_text(edge['label'], center + 16,
                                  start + start_height + 34, 28, 14))
        else:
            lane_index = side_edges.index(edge)
            lane = 760 + lane_index * 11
            source_y = start + start_height / 2
            target_y = end + end_height / 2
            # A self-loop must visibly leave and return to different card points.
            if edge['from'] == edge['to']:
                target_y += 25
            parts.append(f'<path d="M {x + card_width} {source_y} H {lane} '
                         f'V {target_y} H {x + card_width}" fill="none" '
                         f'stroke="{INK}" stroke-width="1.5"/>')
            tip = x + card_width
            parts.append(f'<polygon points="{tip},{target_y} {tip + 11},{target_y - 6} '
                         f'{tip + 11},{target_y + 6}" fill="{INK}"/>')
            parts.append(svg_text(str(lane_index + 1), lane + 3, source_y - 8, 5, 12))
    if side_edges:
        parts.append(svg_text('Branch / return links', 60, y, 80, 18))
        y += 35
        for i, edge in enumerate(side_edges, 1):
            label = f'{i}. {edge["from"]} → {edge["to"]}: {edge["label"]}'
            parts.append(svg_text(label, 60, y, 100, 15))
            y += len(wrapped(label, 100)) * 23 + 15
    return parts, y


def render_matrix(spec, top):
    parts = []
    columns = ['Item', *spec['columns']]
    width = 1080 / len(columns)
    wrap_width = max(12, int((width - 24) / 9))
    height = max(len(wrapped(c, wrap_width)) for c in columns) * 24 + 32
    for i, column in enumerate(columns):
        x = 60 + i * width
        parts.append(rect(x, top, width, height, '#dce9e0', 0))
        parts.append(svg_text(column, x + 12, top + 28, wrap_width, 16))
    y = top + height
    for node in spec['nodes']:
        values = [node['label'], *(cell['text'] for cell in node['cells'])]
        height = max(len(wrapped(v, wrap_width)) for v in values) * 24 + 32
        for i, value in enumerate(values):
            x = 60 + i * width
            parts.append(rect(x, y, width, height, BACKGROUND, 0))
            parts.append(svg_text(value, x + 12, y + 28, wrap_width, 16))
        y += height
    return parts, y + 30


def render_chart(spec, top):
    parts = []
    nodes = spec['nodes']
    maximum = max(n['value'] for n in nodes) or 1
    unit = spec['unit']
    if spec['family'] == 'bar':
        y = top
        parts.append(svg_text(f'Value ({unit}); baseline = 0', 350, y, 70, 14))
        y += 30
        for node in nodes:
            height = max(68, len(wrapped(node['label'], 24)) * 23 + 24)
            parts.append(svg_text(node['label'], 60, y + 23, 24, 16))
            parts.append(rect(350, y, node['value'] / maximum * 600, 32, '#a4c4b1', 0))
            parts.append(svg_text(f'{node["value"]} {unit}', 960, y + 22, 17, 14))
            y += height
        parts.append(f'<path d="M 350 {top + 20} V {y}" stroke="{INK}"/>')
        parts.append(svg_text('0', 346, y + 22, 8, 14))
        return parts, y + 55
    chart_top = top + 30
    bottom = chart_top + 340
    left, right = 140, 1060
    parts.append(svg_text(f'Value ({unit})', 60, top, 80, 14))
    parts.append(f'<path d="M {left} {chart_top} V {bottom} H {right}" '
                 f'stroke="{INK}" fill="none"/>')
    for i in range(5):
        value = maximum * i / 4
        y = bottom - i * 85
        parts.append(svg_text(f'{value:g}', 60, y + 5, 12, 13))
        parts.append(f'<path d="M {left} {y} H {right}" stroke="#dce2dc"/>')
    points = [(left + i * (right - left) / (len(nodes) - 1),
               bottom - node['value'] / maximum * 340) for i, node in enumerate(nodes)]
    points_string = ' '.join(f'{x},{y}' for x, y in points)
    parts.append(f'<polyline points="{points_string}" fill="none" stroke="#527e68" stroke-width="3"/>')
    for i, (node, (x, y)) in enumerate(zip(nodes, points)):
        parts.append(f'<circle cx="{x}" cy="{y}" r="6" fill="#527e68"/>')
        parts.append(svg_text(str(i + 1), x - 4, bottom + 25, 8, 13))
    y = bottom + 65
    # A numbered key keeps long category labels away from plotted points.
    for i, node in enumerate(nodes, 1):
        label = f'{i}. {node["label"]}: {node["value"]} {unit}'
        parts.append(svg_text(label, 60, y, 100, 16))
        y += len(wrapped(label, 100)) * 24 + 12
    parts.append(svg_text('Ordered categories; equal spacing does not imply elapsed time.',
                          60, y, 100, 14))
    return parts, y + 40


def article_links(spec):
    """Retain explicit citations and Markdown links found inside exact anchors."""
    links = [(c['label'], c['url']) for c in spec.get('citations', [])]
    for item in [*spec['nodes'], *spec.get('edges', [])]:
        for label, url in re.findall(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', item['quote']):
            if valid_url(url):
                links.append((label, url))
        for cell in item.get('cells', []):
            for label, url in re.findall(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', cell['quote']):
                if valid_url(url):
                    links.append((label, url))
    return list(dict.fromkeys(links))


def make_svg(spec):
    title_lines = wrapped(spec['title'], 55)
    top = 65 + len(title_lines) * 40 + 30
    parts = [svg_text(spec['title'], 60, 65, 55, 30)]
    if spec['family'] == 'flow':
        body, y = render_flow(spec, top)
    elif spec['family'] == 'matrix':
        body, y = render_matrix(spec, top)
    elif spec['family'] in ('bar', 'line'):
        body, y = render_chart(spec, top)
    else:
        body, y = render_cards(spec, top)
    parts.extend(body)
    y += 20
    parts.append(svg_text('DRAFT · evidence checks only · human review required', 60, y, 100, 14))
    y += 35
    links = article_links(spec) + [(f'Design case {r["case_id"]}', r['article_url'])
                                  for r in spec['case_refs']]
    for label, url in links:
        link_height = len(wrapped(label, 100)) * 20
        parts.append(svg_text(label, 60, y, 100, 14, '#325c88'))
        # MuPDF omits text nested in SVG anchors; overlay a transparent link.
        parts.append(f'<a href="{xml_text(url)}"><rect x="60" y="{y - 16}" '
                     f'width="1080" height="{link_height}" fill="white" fill-opacity="0"/></a>')
        y += link_height + 10
    height = math.ceil(y + 40)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" '
            f'viewBox="0 0 1200 {height}" role="img">'
            f'<title>{xml_text(spec["title"])}</title>'
            f'<rect width="1200" height="{height}" fill="{BACKGROUND}"/>'
            + ''.join(parts) + '</svg>\n')


def make_html(spec, svg):
    links = article_links(spec)
    links.extend((f'Design case {r["case_id"]}', r['article_url']) for r in spec['case_refs'])
    link_html = ''.join(f'<li><a href="{xml_text(url)}">{xml_text(label)}</a></li>'
                        for label, url in links)
    anchors = []
    for node in spec['nodes']:
        anchors.append(f'<li><strong>{xml_text(node["label"])}</strong>'
                       f'<blockquote>{xml_text(node["quote"])}</blockquote></li>')
        for cell in node.get('cells', []):
            anchors.append(f'<li>Cell: {xml_text(cell["text"])}'
                           f'<blockquote>{xml_text(cell["quote"])}</blockquote></li>')
    for edge in spec.get('edges', []):
        anchors.append(f'<li>Link {xml_text(edge["from"])} → {xml_text(edge["to"])}: '
                       f'{xml_text(edge["label"])}'
                       f'<blockquote>{xml_text(edge["quote"])}</blockquote></li>')
    for citation in spec.get('citations', []):
        anchors.append(f'<li>Citation: {xml_text(citation["label"])}'
                       f'<blockquote>{xml_text(citation["quote"])}</blockquote></li>')
    anchors = ''.join(anchors)
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{xml_text(spec["title"])}</title>'
            '<style>body{margin:2rem auto;max-width:1200px;padding:0 1rem;'
            'font-family:system-ui;background:#fcfaf5;color:#243b37}'
            'svg{width:100%;height:auto}blockquote{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
            '<body><p>Draft. Structural QA is not semantic or visual approval.</p>'
            + svg + '<h2>Links</h2><ul>' + link_html + '</ul>'
            '<h2>Exact article anchors</h2><ul>' + anchors + '</ul></body></html>\n')


def render(article, digest, spec, out):
    report = qa(article, digest, spec)
    if not report['ok']:
        raise ValueError('QA failed: ' + '; '.join(report['errors']))
    out = safe_output(out)
    names = ('figure.svg', 'figure.png', 'preview.html', 'figure.json', 'qa.json')
    paths = {name: safe_output(out / name) for name in names}
    try:
        import fitz
    except ImportError as exc:
        raise ValueError('PNG export requires PyMuPDF (fitz); use the pinned requirements.txt') from exc
    svg = make_svg(spec)
    # Render before emitting any files so a missing/broken PNG backend fails clearly.
    with fitz.open(stream=svg.encode('utf-8'), filetype='svg') as document:
        png = document[0].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).tobytes('png')
    outputs = {'figure.svg': svg, 'preview.html': make_html(spec, svg),
               'figure.json': json_text(spec), 'qa.json': json_text(report)}
    out.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        write_text(paths[name], content)
    safe_output(paths['figure.png']).write_bytes(png)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('verify-index', help='Validate local curated records; no network')
    search_parser = sub.add_parser('search', help='Search case evidence, rules and boundaries')
    search_parser.add_argument('query')
    search_parser.add_argument('--family')
    search_parser.add_argument('--include-contrast', action='store_true')
    plan_parser = sub.add_parser('plan', help='Heuristic section inventory, not approval')
    plan_parser.add_argument('article', type=Path)
    plan_parser.add_argument('--budget', type=int, default=3)
    plan_parser.add_argument('--out', type=Path, help='JSON file inside this Skill')
    for command in ('illustrate', 'render', 'qa'):
        cmd = sub.add_parser(command)
        cmd.add_argument('article', type=Path)
        if command != 'illustrate':
            cmd.add_argument('spec', type=Path)
        cmd.add_argument('--out', type=Path, required=command != 'qa',
                         help='Output directory inside this Skill')
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify-index':
            records = load_index()
            result = {'ok': True, 'cases': len(records),
                      'sources': len({r['article_url'] for r in records}),
                      'sha256': hashlib.sha256(INDEX.read_bytes()).hexdigest()}
        elif args.command == 'search':
            result = search(args.query, args.family, args.include_contrast)
        else:
            article, digest = read_article(args.article)
            if args.command == 'plan':
                result = plan(article, digest, args.budget)
                if args.out:
                    write_text(args.out, json_text(result))
            else:
                spec = draft(article, digest) if args.command == 'illustrate' else read_json(args.spec)
                if args.command == 'qa':
                    result = qa(article, digest, spec)
                    if args.out:
                        write_text(safe_output(args.out) / 'qa.json', json_text(result))
                else:
                    result = render(article, digest, spec, args.out)
        print(json_text(result), end='')
        return 1 if isinstance(result, dict) and result.get('ok') is False else 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
