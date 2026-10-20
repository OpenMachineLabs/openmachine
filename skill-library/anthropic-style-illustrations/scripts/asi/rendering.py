"""Deterministic editable vector compositions and mechanical QA, not visual review."""
import html
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from .library import output_path, write_json
from .planning import validate_spec

COLORS = {'ink': '#172b3a', 'muted': '#516374', 'paper': '#f7f9fc', 'blue': '#176b91',
          'teal': '#087f78', 'orange': '#b5522f', 'pale': '#e7eff6', 'white': '#ffffff'}

PALETTES = {
    'engineering': {**COLORS, 'paper': '#ffffff', 'ink': '#213a30', 'muted': '#52665b',
                    'blue': '#436c59', 'teal': '#286b4b', 'pale': '#e8f0e9'},
    'scientific': {**COLORS, 'paper': '#ffffff', 'ink': '#202630', 'muted': '#555d69',
                   'blue': '#3b5b8a', 'teal': '#466966', 'pale': '#edf0f3'},
    'editorial': COLORS,
}


def width(text, size):
    # Conservative width for wrapping; no labels are clipped or shortened.
    return sum((1.05 if unicodedata.east_asian_width(c) in 'WF' else .68) * size for c in text)


def wrap(text, max_width, size):
    lines, line = [], ''
    for word in re.findall(r'\S+|\n', str(text)):
        if word == '\n':
            lines.append(line)
            line = ''
            continue
        candidate = (line + ' ' + word).strip()
        if width(candidate, size) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ''
        for char in word:
            if line and width(line + char, size) > max_width:
                lines.append(line)
                line = ''
            line += char
    if line:
        lines.append(line)
    return lines or ['']


def reject_active(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() in ('script', 'onclick', 'onload', 'foreignobject', 'href', 'style'):
                raise ValueError('Active content fields are not supported')
            reject_active(v)
    elif isinstance(value, list):
        for v in value:
            reject_active(v)
    elif isinstance(value, str):
        if re.search(r'<\s*(script|iframe|object|foreignobject)\b|(?:javascript|data|file):|url\s*\(', value, re.I):
            raise ValueError('Unsafe script or URL in figure spec')
        for url in re.findall(r'\b[a-zA-Z]+://[^\s<>"\']+', value):
            from .library import safe_url
            safe_url(url)


class Canvas:
    def __init__(self, title, alt, width_value=1200, palette=None):
        self.colors = palette or COLORS
        self.width = width_value
        self.parts = []
        self.boxes = []
        self.text_boxes = []
        self.routes = []
        self.title = title
        self.alt = alt

    def rect(self, x, y, w, h, fill='white', stroke='none', radius=12):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{self.colors.get(fill, fill)}" stroke="{self.colors.get(stroke, stroke)}"/>')

    def text(self, text, x, y, w, size=18, color='ink', weight=400):
        lines = wrap(text, w, size)
        height = len(lines) * size * 1.45
        self.text_boxes.append([x, y, w, height])
        spans = ''.join(f'<tspan x="{x}" dy="{0 if i == 0 else size*1.45}">{html.escape(line)}</tspan>' for i, line in enumerate(lines))
        self.parts.append(f'<text x="{x}" y="{y+size}" font-family="sans-serif" font-size="{size}" font-weight="{weight}" fill="{self.colors[color]}">{spans}</text>')
        return height

    def route(self, points, label=None):
        self.routes.append(points)
        coordinates = ' '.join(f'{x},{y}' for x, y in points)
        self.parts.append(f'<polyline points="{coordinates}" fill="none" stroke="{self.colors["blue"]}" stroke-width="2.5"/>')
        # Explicit geometry survives SVG-to-PNG engines that omit SVG markers.
        x, y = points[-1]
        px, py = next(p for p in reversed(points[:-1]) if p != points[-1])
        length = math.hypot(x-px, y-py)
        dx, dy = (x-px)/length, (y-py)/length
        arrow = [(x, y), (x-10*dx+5*dy, y-10*dy-5*dx), (x-10*dx-5*dy, y-10*dy+5*dx)]
        self.parts.append('<polygon points="'+' '.join(f'{a},{b}' for a,b in arrow)+f'" fill="{self.colors["blue"]}"/>')
        if label:
            raise ValueError('Use caption annotations for edge semantics; edge labels are not silently dropped')

    def svg(self, height):
        all_boxes = self.boxes + self.text_boxes
        if height > 12000 or any(x < 24 or y < 16 or x+w > self.width-24 or y+h > height-24 for x, y, w, h in all_boxes):
            raise ValueError('Geometry overflow; simplify or split this figure')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{height}" viewBox="0 0 {self.width} {height}" role="img" aria-labelledby="title description">'
                f'<title id="title">{html.escape(self.title)}</title><desc id="description">{html.escape(self.alt)}</desc>'
                f'<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="{self.colors["blue"]}"/></marker></defs>'
                f'<rect width="100%" height="100%" fill="{self.colors["paper"]}"/>' + ''.join(self.parts) + '</svg>')


def unique(items):
    ids = [item.get('id') for item in items]
    if any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z][\w-]*', i) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Visual IDs must be unique safe identifiers')


def compose(spec):
    reject_active(spec)
    style = spec.get('palette', 'editorial')
    if style not in PALETTES:
        raise ValueError('Unknown palette')
    colors = PALETTES[style]
    c = Canvas(spec['title'], spec['alt'], palette=colors)
    c.rect(48, 32, 58, 6, 'teal', radius=3)
    top = 62 + c.text(spec['title'], 48, 56, 1104, 32, weight=700)
    if spec.get('subtitle'):
        top += c.text(spec['subtitle'], 48, top+12, 1104, 18, 'muted') + 20
    top += 48
    family = spec['family']
    if family == 'flow':
        nodes = spec.get('nodes', [])
        if not nodes:
            raise ValueError('Flow requires explicit nodes and edges')
        unique(nodes)
        positions = set()
        for n in nodes:
            if not isinstance(n.get('row'), int) or not isinstance(n.get('column'), int) or not 0 <= n['row'] <= 12 or not 0 <= n['column'] <= 4:
                raise ValueError('Node row/column must be bounded nonnegative integers')
            pos = n['row'], n['column']
            if pos in positions or not n.get('label'):
                raise ValueError('Duplicate node position or missing label')
            positions.add(pos)
        cols = 1 + max(n['column'] for n in nodes)
        box_width = (1104 - 72*(cols-1)) / cols
        box_height = max(100, max(len(wrap(n['label'], box_width-32, 18))*27+52 for n in nodes))
        geometry = {n['id']: [48+n['column']*(box_width+72), top+n['row']*(box_height+84), box_width, box_height] for n in nodes}
        bottom = max(y+h for x, y, w, h in geometry.values())
        edges = spec.get('edges', [])
        if not edges:
            raise ValueError('Flow requires edges')
        edge_ids = set()
        for index, edge in enumerate(edges):
            a, b = edge.get('from'), edge.get('to')
            if a not in geometry or b not in geometry or (a, b) in edge_ids:
                raise ValueError('Unknown endpoint or duplicate edge')
            edge_ids.add((a, b))
            if not edge.get('meaning') or not edge.get('claim_ids') or any(k not in {r['id'] for r in spec['claims']} for k in edge['claim_ids']):
                raise ValueError('Edges require source-bound meaning')
            x, y, w, h = geometry[a]
            xx, yy, ww, hh = geometry[b]
            if xx == x+w+72:
                points = [(x+w, y+h/2), (x+w+36, y+h/2), (x+w+36, yy+hh/2), (xx, yy+hh/2)]
            else:
                # Loop/skip edges use separate outside lanes, avoiding node interiors.
                lane = bottom+32+index*22
                points = [(x+w, y+h/2), (x+w+20, y+h/2), (x+w+20, lane), (xx-20, lane), (xx-20, yy+hh/2), (xx, yy+hh/2)]
                if x+w+20 > 1176 or xx-20 < 24:
                    raise ValueError('Loop routing exceeds margins')
            for p, q in zip(points, points[1:]):
                for nx, ny, nw, nh in geometry.values():
                    if (p[0] == q[0] and nx < p[0] < nx+nw and max(min(p[1], q[1]), ny) < min(max(p[1], q[1]), ny+nh)) or (p[1] == q[1] and ny < p[1] < ny+nh and max(min(p[0], q[0]), nx) < min(max(p[0], q[0]), nx+nw)):
                        raise ValueError('Connector crosses a node; rearrange explicit rows/columns')
            c.route(points)
        for index, node in enumerate(nodes):
            x, y, w, h = geometry[node['id']]
            c.boxes.append([x, y, w, h])
            c.rect(x, y, w, h, 'white', 'pale')
            c.rect(x, y, 5, h, 'teal' if index % 2 == 0 else 'blue', radius=2)
            c.text(node['label'], x+16, y+20, w-32, 18, weight=600)
        bottom = max(bottom, max(p[1] for r in c.routes for p in r))
        names = {n['id']: n['label'].split(' • ')[0] for n in nodes}
        annotation = '\n'.join(f'{names[e["from"]]} → {names[e["to"]]}: {e["meaning"]}' for e in edges)
        bottom += 24 + c.text(annotation, 48, bottom+24, 1104, 15, 'muted')
    elif family in ('comparison', 'matrix'):
        items = spec.get('items', [])
        if not items:
            raise ValueError('Comparison/matrix requires items')
        unique(items)
        columns = spec.get('columns', [])
        if not 2 <= len(columns) <= 5 or not all(isinstance(x, str) and x for x in columns):
            raise ValueError('Provide two to five column headings')
        w = 1104 / len(columns)
        header_height = max(len(wrap(v, w-32, 19))*28+32 for v in columns)
        for i, label in enumerate(columns):
            c.rect(48+i*w, top, w-4, header_height, 'ink', radius=4)
            c.text(label, 64+i*w, top+12, w-36, 19, 'white', 700)
        bottom = top+header_height+8
        for row, item in enumerate(items):
            cells = item.get('cells', [])
            if len(cells) != len(columns) or not all(isinstance(v, str) and v for v in cells):
                raise ValueError('Each row must have one nonempty cell per column')
            h = max(len(wrap(v, w-36, 18))*27+32 for v in cells)
            for i, value in enumerate(cells):
                c.rect(48+i*w, bottom, w-4, h, 'white' if row % 2 == 0 else 'pale', radius=3)
                c.text(value, 64+i*w, bottom+12, w-36)
            bottom += h+6
    elif family in ('bar', 'line'):
        data = spec.get('data', [])
        if not data or len(data) > 30 or (family == 'line' and len(data) < 2):
            raise ValueError('Charts need 1–30 real data points (line: at least two)')
        unique(data)
        unit = spec.get('unit')
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError('Chart needs a unit')
        claims = {r['id']: r for r in spec['claims']}
        for item in data:
            value = item.get('value')
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError('Chart values must be finite nonnegative real numbers')
            bound_quotes = [claims[k]['quote'] for k in item['claim_ids']]
            evidence = item.get('evidence')
            if not isinstance(evidence, str) or not evidence or not any(evidence in quote for quote in bound_quotes):
                raise ValueError('Each chart point needs an exact narrow evidence span from one bound claim')
            label = item.get('label')
            if not isinstance(label, str) or not label or label not in evidence:
                raise ValueError('Chart label must occur in its evidence span')
            numeric_evidence = evidence.replace(label, '', 1)
            numbers = [float(x.replace(',', '')) for x in re.findall(r'(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?![\w.])', numeric_evidence)]
            expected_numbers = [item.get('x'), value] if family == 'line' else [value]
            if numbers != expected_numbers or unit not in evidence:
                raise ValueError('Chart label, value and unit must occur together in input evidence')
            if family == 'line' and (not isinstance(item.get('x'), (int, float)) or isinstance(item['x'], bool) or not math.isfinite(item['x']) or item['x'] not in numbers):
                raise ValueError('Line x values must be finite and input-bound')
        maximum = max(d['value'] for d in data) or 1
        if family == 'bar':
            bottom = top
            for i, item in enumerate(data):
                h = max(54, len(wrap(item['label'], 270, 17))*25+16, len(wrap(f'{item["value"]} {unit}', 168, 17))*25+16)
                c.text(item['label'], 48, bottom+8, 270, 17)
                c.rect(340, bottom+12, 630*(item['value']/maximum), 24, 'teal' if i % 2 == 0 else 'blue', radius=3)
                c.text(f'{item["value"]} {unit}', 984, bottom+8, 168, 17)
                bottom += h
            c.text('0 • Shared zero baseline', 340, bottom+10, 600, 15, 'muted')
            bottom += 42
        else:
            xs = [d['x'] for d in data]
            if xs != sorted(set(xs)) or not spec.get('x_unit'):
                raise ValueError('Line needs strictly increasing x values and x_unit')
            if any(spec['x_unit'] not in ' '.join(claims[k]['quote'] for k in item['claim_ids']) for item in data):
                raise ValueError('Line x unit is not input-bound')
            if not math.isfinite(xs[-1]-xs[0]):
                raise ValueError('Line x range exceeds numeric geometry limits')
            coords = [(130+940*((d['x']-xs[0])/(xs[-1]-xs[0])), top+320-280*(d['value']/maximum)) for d in data]
            c.parts.append(f'<path d="M130,{top+20} V{top+320} H1100" stroke="{colors["muted"]}" fill="none"/>')
            c.parts.append('<polyline points="'+' '.join(f'{x},{y}' for x,y in coords)+f'" stroke="{colors["teal"]}" stroke-width="3" fill="none"/>')
            c.text(unit, 130, top-20, 970, 16)
            for value in sorted({0, maximum/2, maximum}):
                yy = top+320-280*value/maximum
                c.parts.append(f'<path d="M124,{yy} H130" stroke="{colors["muted"]}"/>')
                c.text(f'{value:g}', 62, yy-12, 60, 16)

            for x, y in coords:
                c.parts.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{colors["teal"]}"/>')
            # Bound tick density; the full data table below retains every value.
            for i in sorted({0, len(data)//2, len(data)-1}):
                xx = coords[i][0]
                c.parts.append(f'<path d="M{xx},{top+320} v6" stroke="{colors["muted"]}"/>')
                c.text(f'{xs[i]:g}', xx-12, top+330, 90, 16)
            c.text(f'Source position ({spec["x_unit"]})', 130, top+365, 970, 16, 'muted')
            bottom = top+405
            # Complete data table avoids crowded or silently shortened point labels.
            for item in data:
                label = f'{item["label"]}: {item["x"]} {spec["x_unit"]} → {item["value"]} {unit}'
                bottom += c.text(label, 130, bottom, 970, 16)+6
    else:
        items = spec.get('items', [])
        if not 1 <= len(items) <= 4:
            raise ValueError('Conceptual cover needs one to four original labeled motifs')
        unique(items)
        bottom = top
        for i, item in enumerate(items):
            if not item.get('label'):
                raise ValueError('Cover motif needs a label')
            h = max(120, len(wrap(item['label'], 800, 24))*35+42)
            c.parts.append(f'<circle cx="130" cy="{bottom+h/2}" r="44" fill="{colors["teal" if i%2 == 0 else "blue"]}"/>')
            c.text(str(i+1), 114, bottom+h/2-22, 48, 30, 'white', 700)
            c.text(item['label'], 226, bottom+30, 880, 24, weight=600)
            bottom += h+18
        bottom += c.text('Conceptual illustration • No quantitative encoding', 48, bottom+8, 1104, 16, 'muted')+20
    bottom += 36
    c.rect(48, bottom, 1104, 2, 'pale', radius=0)
    bottom += 18+c.text(spec['caption'], 48, bottom+18, 1104, 17)
    if spec.get('annotations'):
        bottom += 16+c.text(' • '.join(spec['annotations']), 48, bottom+16, 1104, 16, 'muted')
    if spec.get('references'):
        urls = dict.fromkeys(r['source_url'] for r in spec['references'])
        source_note = 'Reference context: ' + ' ; '.join(urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1].replace('-', ' ').capitalize() for url in urls)
        bottom += 16+c.text(source_note, 48, bottom+16, 1104, 13, 'muted')
    height = math.ceil(bottom+48)
    svg = c.svg(height)
    report = {'status': 'PASS', 'checks': ['input_hash', 'claim_anchors', 'reference_hashes', 'unique_ids', 'numeric_evidence', 'geometry', 'escaped_text'],
              'width': c.width, 'height': height, 'text_blocks': len(c.text_boxes), 'nodes': len(c.boxes),
              'visual_review': 'NOT RUN', 'semantic_review': spec.get('semantic_review', 'NOT RUN'),
              'limits': 'Mechanical source anchors do not prove paraphrase entailment, aesthetic quality or inspection truth.'}
    return svg, report


def render(spec, text, knowledge, out, *, png=True):
    validate_spec(spec, text, knowledge)
    svg, report = compose(spec)
    out = output_path(out)
    out.mkdir(parents=True, exist_ok=True)
    links = '<ul>' + ''.join('<li><a href="'+html.escape(url, quote=True)+'">'+html.escape(url)+'</a></li>' for url in dict.fromkeys(r['source_url'] for r in spec.get('references', []))) + '</ul>'
    payloads = {'figure.svg': svg, 'preview.html': '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:"><title>'+html.escape(spec['title'])+'</title><style>body{margin:0;background:#e7eff6;padding:24px}svg{width:100%;height:auto;max-width:1200px;display:block;margin:auto}</style>'+svg+links+'</html>'}
    for name, value in payloads.items():
        output_path(out/name).write_text(value, encoding='utf-8')
    write_json(out/'figure.json', spec)
    if png:
        import fitz
        with fitz.open(stream=svg.encode('utf-8'), filetype='svg') as document:
            with fitz.open('pdf', document.convert_to_pdf()) as pdf:
                pdf[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).save(str(output_path(out/'figure.png')))
    write_json(out/'qa.json', report)
    return report


def qa(spec, text, knowledge, directory=None):
    validate_spec(spec, text, knowledge)
    svg, report = compose(spec)
    if directory:
        directory = Path(directory)
        if (directory/'figure.svg').read_text(encoding='utf-8') != svg:
            raise ValueError('SVG does not match the validated editable spec')
        root = ET.fromstring(svg)
        ids = [n.attrib['id'] for n in root.iter() if 'id' in n.attrib]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate SVG IDs')
    return report
