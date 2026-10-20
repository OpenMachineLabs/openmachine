"""Read one official publication transiently; retain fingerprints, not full source."""
import hashlib
from bs4 import BeautifulSoup
from .corpus import SafeFetcher
from .library import digest, official_url, safe_url
from .planning import sections


def read_publication(url, *, network="direct", fetcher=None):
    safe_url(url)
    if not official_url(url):
        raise ValueError('Full-publication context requires an official publication URL')
    from urllib.parse import urlsplit
    client = fetcher or SafeFetcher({urlsplit(url).hostname}, network=network, retries=0)
    response = client.get(url)
    soup = BeautifulSoup(response.body, 'html.parser')
    scope = soup.find('article') or soup.find('main')
    if scope is None:
        raise ValueError('No editorial article/main scope; host must identify the content')
    for node in scope.select('script,style,nav,footer,form'):
        node.decompose()
    lines = []
    for tag in scope.find_all(['h1','h2','h3','h4','h5','h6','p','li','pre','figcaption','table']):
        if tag.find_parent(['p','li','pre','figcaption','table']):
            continue
        text = tag.get_text(' ',strip=True)
        if text:
            lines.append(('#' * int(tag.name[1]) + ' ' if tag.name.startswith('h') else '') + text)
    text = '\n\n'.join(lines)
    if not text.strip():
        raise ValueError('Empty publication')
    return text, {'url':url, 'response_sha256':hashlib.sha256(response.body).hexdigest(),
                  'input_hash':digest(text), 'transport':f'SafeFetcher(network={network})' if fetcher is None else 'injected_fetcher',
                  'full_source_retained':False}


def coverage(text):
    """Hash every section without copying headings or paragraphs into an artifact."""
    return [{'section_id':p['id'], 'heading_sha256':digest(p['heading']),
             'section_sha256':digest(p['text']), 'words':len(p['text'].split()),
             'start_line':p['start_line'], 'end_line':p['end_line']} for p in sections(text)]
