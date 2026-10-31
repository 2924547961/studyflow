"""Decode observed inline Chaoxing fonts offline; never fetch or mutate a page."""
import json
from pathlib import Path
import re
import shutil
import subprocess

from bs4 import BeautifulSoup


def decode_observed_font(soup: BeautifulSoup) -> dict:
    nodes = soup.select('.font-cxsecret')
    if not nodes:
        return {'status': 'not_required', 'decoded_nodes': 0}
    fonts = set()
    for style in soup.select('style'):
        css = style.get_text()
        if 'font-cxsecret' in css:
            fonts.update(re.findall(r'data:[^\s\"\']*?;base64,([A-Za-z0-9+/=]+)', css))
    if len(fonts) != 1:
        return {'status': 'blocked', 'reason': 'exactly one observed inline font required', 'decoded_nodes': 0}
    runtime = shutil.which('node')
    if not runtime:
        return {'status': 'blocked', 'reason': 'offline Node runtime missing', 'decoded_nodes': 0}
    text = ''.join(n.get_text() for n in nodes)
    try:
        result = subprocess.run([runtime, str(Path(__file__).parent / 'assets' / 'decode_font.cjs')],
                                input=json.dumps({'font_base64': next(iter(fonts)), 'text': text}),
                                text=True, encoding='utf-8', capture_output=True, timeout=15, check=True)
        decoded = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'status': 'blocked', 'reason': 'offline font decoder failed', 'decoded_nodes': 0}
    if decoded.get('unresolved') or not decoded.get('mapping'):
        return {'status': 'blocked', 'reason': 'unmapped glyphs; do not guess',
                'unresolved': decoded.get('unresolved', []), 'decoded_nodes': 0}
    translation = str.maketrans(decoded['mapping'])
    # Collect text nodes once, including nested encoded spans, before mutation.
    strings = {id(s): s for n in nodes for s in n.find_all(string=True)}
    for string in strings.values():
        string.replace_with(str(string).translate(translation))
    for node in nodes:
        node['class'] = [c for c in node.get('class', []) if c != 'font-cxsecret']
    return {'status': 'decoded', 'decoded_nodes': len(nodes), 'font_sha256': decoded['font_sha256'],
            'evidence': 'offline glyph fingerprint mapping of controller-observed inline font; not an answer key'}
