"""Small, read-only DOCX -> HTML preview used by authenticated attachment routes.

The source document is parsed with python-docx and every text fragment is HTML
escaped.  This is intentionally a viewer, not a round-trip converter: paragraphs,
basic emphasis, lists, and tables are preserved while active document content is
never emitted.
"""
from html import escape

from docx import Document
from docx.document import Document as _Document
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph


def _blocks(parent):
    root = parent.element.body if isinstance(parent, _Document) else parent._tc
    for child in root.iterchildren():
        if child.tag.endswith('}p'):
            yield Paragraph(child, parent)
        elif child.tag.endswith('}tbl'):
            yield Table(child, parent)


def _run_html(run):
    text = escape(run.text or '').replace('\n', '<br>')
    if not text:
        return ''
    if run.bold:
        text = f'<strong>{text}</strong>'
    if run.italic:
        text = f'<em>{text}</em>'
    if run.underline:
        text = f'<u>{text}</u>'
    return text


def _paragraph_html(paragraph):
    body = ''.join(_run_html(run) for run in paragraph.runs)
    if not body:
        body = escape(paragraph.text or '')
    style = (paragraph.style.name if paragraph.style else '') or ''
    if style.startswith('Heading'):
        try:
            level = min(6, max(1, int(style.split()[-1])))
        except (ValueError, IndexError):
            level = 2
        return f'<h{level}>{body}</h{level}>'
    if style.startswith('List'):
        return f'<p class="list-item">{body}</p>'
    return f'<p>{body or "&nbsp;"}</p>'


def _cell_html(cell):
    return ''.join(_block_html(block) for block in _blocks(cell)) or '&nbsp;'


def _table_html(table):
    rows = []
    for row in table.rows:
        rows.append('<tr>' + ''.join(f'<td>{_cell_html(cell)}</td>' for cell in row.cells) + '</tr>')
    return '<table><tbody>' + ''.join(rows) + '</tbody></table>'


def _block_html(block):
    return _paragraph_html(block) if isinstance(block, Paragraph) else _table_html(block)


def render_docx_html(path, title):
    doc = Document(path)
    body = ''.join(_block_html(block) for block in _blocks(doc))
    safe_title = escape(title or 'DOCX 미리보기')
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title><style>
body{{margin:0;background:#e9edf2;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.toolbar{{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;padding:12px 18px;background:#fff;border-bottom:1px solid #d9dee7}}
.toolbar strong{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.toolbar a{{margin-left:auto;color:#0b5f83;text-decoration:none;white-space:nowrap}}
.page{{box-sizing:border-box;max-width:900px;min-height:calc(100vh - 80px);margin:18px auto;padding:54px 64px;background:#fff;box-shadow:0 2px 14px #0002}}
p{{margin:.45em 0;line-height:1.55;white-space:pre-wrap}}.list-item{{padding-left:1.2em}}h1,h2,h3,h4,h5,h6{{margin:1.1em 0 .45em}}
table{{width:100%;border-collapse:collapse;margin:1em 0}}td{{border:1px solid #aeb6c2;padding:6px 8px;vertical-align:top}}td p{{margin:.15em 0}}
@media(max-width:700px){{.page{{margin:0;padding:28px 18px;box-shadow:none}}.toolbar{{padding:10px 12px}}}}
</style></head><body><div class="toolbar"><strong>{safe_title}</strong><a href="?download=1">원본 다운로드</a></div><main class="page">{body}</main></body></html>'''
