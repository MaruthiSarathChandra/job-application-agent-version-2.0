"""Escaped HTML reading preview of the actual stored DOCX; no embedded active content."""
import html
import io
from urllib.parse import urlsplit

def preview_html(raw):
    from docx import Document
    from docx.text.hyperlink import Hyperlink
    doc=Document(io.BytesIO(raw))
    def esc(text):
        return html.escape(text).replace('\n','<br>').replace('\t','<span class="tab"></span>')
    def run_html(run):
        value=esc(run.text)
        if run.bold: value='<b>'+value+'</b>'
        if run.italic: value='<i>'+value+'</i>'
        return value
    parts=[]
    for paragraph in doc.paragraphs:
        content=[]
        for item in paragraph.iter_inner_content():
            if isinstance(item,Hyperlink):
                label=''.join(run_html(r) for r in item.runs)
                try:
                    url=urlsplit(item.url)
                    safe=url.scheme in ('http','https') and url.hostname and not url.username and not url.password
                except ValueError:
                    safe=False
                content.append('<a href="'+html.escape(item.url,quote=True)+'" target="_blank" rel="noopener noreferrer">'+label+'</a>' if safe else label)
            else:
                content.append(run_html(item))
        style=paragraph.style.name
        cls='title' if style=='Title' else 'section' if style=='Heading 1' else 'bullet' if style=='List Bullet' else ''
        if paragraph.alignment==1: cls+=' centered'
        parts.append('<p class="'+cls+'">'+''.join(content)+'</p>')
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{margin:0;background:#edf0f5;font:14px Calibri,Arial,sans-serif;color:#111}
    article{max-width:850px;margin:20px auto;background:white;padding:40px;box-sizing:border-box}
    p{margin:4px 0;line-height:1.4;overflow-wrap:anywhere}.title{font-size:30px;font-weight:bold}
    .centered{text-align:center}.section{font-size:16px;font-weight:bold;color:#203e60;border-bottom:2px solid #203e60;margin-top:15px}
    .bullet{padding-left:18px;position:relative}.bullet:before{content:'•';position:absolute;left:3px}
    a{color:#203e60}.tab{display:inline-block;min-width:2em}
    @media(max-width:600px){article{padding:20px;margin:0}}</style></head><body><article>"""+''.join(parts)+"</article></body></html>"
