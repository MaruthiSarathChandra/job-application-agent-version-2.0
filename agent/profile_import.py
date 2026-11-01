"""Read a simple paragraph-based DOCX into a reviewable profile, without model calls."""
import io
import uuid
from zipfile import ZipFile, BadZipFile

HEADINGS = {'SUMMARY':'Summary', 'PROFESSIONAL SUMMARY':'Summary',
            'TECHNICAL SKILLS':'Skills', 'SKILLS':'Skills',
            'PROFESSIONAL EXPERIENCE':'Experience', 'EXPERIENCE':'Experience',
            'SELECTED ENGINEERING PROJECTS':'Projects', 'PROJECTS':'Projects',
            'EDUCATION':'Education'}

def validate_layout(fact):
    layout = fact.get('layout')
    if layout is None:
        return
    if not isinstance(layout, list) or not 1 <= len(layout) <= 100:
        raise ValueError('Entry layout must have 1–100 paragraphs')
    from .resume_artifact import validate_header
    for block in layout:
        if not isinstance(block, dict) or set(block) - {'text','kind','links'}:
            raise ValueError('Invalid resume paragraph')
        if block.get('kind') not in ('heading','body','bullet','metadata'):
            raise ValueError('Invalid resume paragraph style')
        if not isinstance(block.get('text'), str) or not block['text'].strip():
            raise ValueError('Resume paragraph needs text')
        links = block.get('links', [])
        validate_header({'display_name':'validation', 'links':links})
        if any(link['label'] not in block['text'] for link in links):
            raise ValueError('Link label is missing from paragraph')
    if '\n'.join(block['text'] for block in layout) != fact['text']:
        raise ValueError('Resume paragraph layout differs from entry text')

def import_docx(raw):
    from docx import Document
    from lxml.etree import XMLSyntaxError
    from .resume_artifact import dependency_check
    dependency_check()
    if len(raw) > 2_000_000:
        raise ValueError('Resume upload exceeds 2 MB')
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 500 or sum(x.file_size for x in archive.infolist()) > 10_000_000:
                raise ValueError('Resume archive is too large')
            if 'word/document.xml' not in archive.namelist():
                raise ValueError('Upload a DOCX resume')
        doc = Document(io.BytesIO(raw))
    except (BadZipFile, KeyError, OSError, ValueError, XMLSyntaxError) as exc:
        raise ValueError('Cannot read this DOCX resume: use a valid, unencrypted DOCX under 2 MB') from exc
    if doc.tables:
        raise ValueError('Import currently supports paragraph-based resumes; table layouts need manual entry')
    paragraphs = [p for p in doc.paragraphs if p.text.strip()]
    if len(paragraphs) > 500:
        raise ValueError('Resume has too many paragraphs')
    first = next((i for i,p in enumerate(paragraphs) if p.text.strip().upper() in HEADINGS), None)
    if first is None or first < 1:
        raise ValueError('Resume needs a name and recognized Summary, Skills, Experience, Projects, or Education headings')
    def links(p):
        return [{'label':h.text,'url':h.url} for h in p.hyperlinks if h.text and h.url]
    header = {'display_name':paragraphs[0].text, 'contact_lines':[], 'links':[]}
    # Reference layout: name, optional headline, contact paragraph.
    pre = paragraphs[1:first]
    if len(pre) >= 2:
        header['headline'] = pre[0].text
        pre = pre[1:]
    for p in pre:
        text = p.text
        for link in links(p):
            header['links'].append(link)
            text = text.replace(link['label'], '', 1)
        text = text.strip(' |\t')
        if text:
            header['contact_lines'].append(text)
    facts, section, group = [], None, None
    for p in paragraphs[first:]:
        text = p.text.strip()
        if text.upper() in HEADINGS:
            section = HEADINGS[text.upper()]
            group = None
            continue
        bullet = p.style.name.startswith('List Bullet') or p._p.find('.//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr') is not None
        bold = any(r.bold for r in p.runs if r.text.strip())
        if section in ('Experience','Projects'):
            kind = 'bullet' if bullet else 'heading' if bold else 'metadata'
        else:
            kind = 'body'
        if group is None or kind == 'heading' or section not in ('Experience','Projects'):
            group = {'id':str(uuid.uuid4()),'section':section,'text':'','verified':False,'layout':[]}
            facts.append(group)
        block = {'kind':kind,'text':text}
        if links(p):
            block['links'] = links(p)
        group['layout'].append(block)
        group['text'] = '\n'.join(b['text'] for b in group['layout'])
    if not facts:
        raise ValueError('No resume entries found')
    from .resume_artifact import validate_header
    validate_header(header)
    for fact in facts:
        validate_layout(fact)
        if len(fact['text']) > 10000:
            raise ValueError('An imported entry exceeds 10,000 characters')
    return {'profile':{'profile_id':str(uuid.uuid4()),'verified':False,'resume_header':header,'facts':facts},
            'notice':'Imported locally. Review all claims, dates, links, and section assignments before saving. No GPT call was made.'}
