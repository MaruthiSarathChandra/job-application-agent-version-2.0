"""Deterministic DOCX artifact storage and Agent 2 -> Agent 3 handoff."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit
import uuid
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

TEMPLATE_VERSION = 'times-new-roman-v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS resume_artifacts(
 id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 profile_version TEXT NOT NULL, template_version TEXT NOT NULL, validation TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS application_queue(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL REFERENCES jobs(id),
 resume_artifact_id TEXT NOT NULL REFERENCES resume_artifacts(id),
 status TEXT NOT NULL DEFAULT 'ready_for_application', payload TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS render_tasks(
 queue_order INTEGER PRIMARY KEY REFERENCES resume_queue(sequence),
 status TEXT NOT NULL DEFAULT 'waiting', attempts INTEGER NOT NULL DEFAULT 0,
 worker_token TEXT, lease_until REAL, next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT);
'''


def validate_header(header):
    if not isinstance(header, dict) or not isinstance(header.get('display_name'), str) or not header['display_name'].strip():
        raise ValueError('Add your verified resume_header.display_name to the local profile')
    if len(header['display_name']) > 150:
        raise ValueError('Resume name exceeds length limit')
    contacts = header.get('contact_lines', [])
    links = header.get('links', [])
    if not isinstance(contacts, list) or len(contacts) > 5 or any(not isinstance(x, str) or not x.strip() or len(x)>200 for x in contacts):
        raise ValueError('resume_header.contact_lines must contain up to 5 nonempty short strings')
    if not isinstance(links, list) or len(links) > 10:
        raise ValueError('resume_header.links must contain up to 10 links')
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get('label'), str) or not link['label'].strip() or len(link['label'])>100:
            raise ValueError('Every resume header link needs a short label')
        if not isinstance(link.get('url'), str) or len(link['url'])>1000:
            raise ValueError('Every resume header link needs an HTTP(S) URL')
        url = urlsplit(link['url'])
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
            raise ValueError('Resume links must be HTTP(S) URLs without credentials')
    return {'display_name': header['display_name'], 'contact_lines': contacts, 'links': links}


def dependency_check():
    try:
        import docx
    except ImportError:
        raise ValueError('Install DOCX support with: python -m pip install -r requirements.txt') from None


def add_link(paragraph, text, url):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE
    relationship = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), relationship)
    run = OxmlElement('w:r')
    properties = OxmlElement('w:rPr')
    font = OxmlElement('w:rFonts')
    font.set(qn('w:ascii'), 'Times New Roman')
    font.set(qn('w:hAnsi'), 'Times New Roman')
    properties.append(font)
    run.append(properties)
    value = OxmlElement('w:t')
    value.text = text
    run.append(value)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def add_fact(paragraph, text):
    start = 0
    for match in re.finditer(r'https?://[^\s<>]+', text):
        paragraph.add_run(text[start:match.start()])
        full = match.group()
        target = full.rstrip('.,;)]}')
        add_link(paragraph, target, target)
        paragraph.add_run(full[len(target):])
        start = match.end()
    paragraph.add_run(text[start:])


def canonical_zip(raw):
    """Stable metadata lets identical resume content reuse a content-addressed artifact."""
    output = io.BytesIO()
    with ZipFile(io.BytesIO(raw)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as result:
        for name in sorted(source.namelist()):
            info = ZipInfo(name, (2000, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            result.writestr(info, source.read(name))
    return output.getvalue()


def render_docx(draft, header):
    dependency_check()
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    header = validate_header(header)
    sections = draft.get('sections', [])
    if not sections or not any(section.get('facts') for section in sections):
        raise ValueError('Cannot render an empty resume')
    texts = [fact['text'] for section in sections for fact in section['facts']]
    if sum(len(text.split()) for text in texts)>650:
        raise ValueError('Selected facts exceed the 650-word resume budget; shorten verified facts or select fewer')
    document = Document()
    page = document.sections[0]
    page.page_width, page.page_height = Inches(8.5), Inches(11)
    page.top_margin = page.bottom_margin = Inches(0.55)
    page.left_margin = page.right_margin = Inches(0.65)
    for name in ('Normal', 'Title', 'Heading 1', 'List Bullet'):
        style = document.styles[name]
        style.font.name = 'Times New Roman'
        fonts=style.element.get_or_add_rPr().rFonts
        for attribute in list(fonts.attrib):
            if 'theme' in attribute.lower():
                del fonts.attrib[attribute]
        for script in ('ascii','hAnsi','eastAsia','cs'):
            fonts.set(qn('w:'+script),'Times New Roman')
        for border in list(style.element.iter(qn('w:pBdr'))):
            border.getparent().remove(border)
        style.font.size = Pt(11)
        style.font.color.rgb = RGBColor(0,0,0)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.line_spacing = 1.0
    document.styles['Heading 1'].font.bold = True
    document.styles['Heading 1'].font.size = Pt(11)
    document.styles['Heading 1'].paragraph_format.space_before = Pt(8)
    title = document.add_paragraph(style='Title')
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.add_run(header['display_name']).bold = True
    title.runs[0].font.size = Pt(17)
    for line in header['contact_lines']:
        paragraph = document.add_paragraph(line)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Pt(2)
    if header['links']:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for index, link in enumerate(header['links']):
            if index:
                paragraph.add_run(' | ')
            add_link(paragraph, link['label'], link['url'])
    for section in sections:
        document.add_paragraph(section['title'], style='Heading 1')
        for fact in section['facts']:
            paragraph = document.add_paragraph(style='List Bullet' if section['title'] in ('Experience','Projects') else 'Normal')
            paragraph.paragraph_format.keep_together = True
            add_fact(paragraph, fact['text'])
    metadata = document.core_properties
    metadata.author = ''
    metadata.last_modified_by = ''
    metadata.title = header['display_name']
    metadata.created = metadata.modified = datetime(2000,1,1,tzinfo=timezone.utc)
    metadata.revision = 1
    output = io.BytesIO()
    document.save(output)
    return canonical_zip(output.getvalue())


def validate_docx(raw, draft, header):
    """Structural/content validation only; no claim of runtime visual/page-count validation."""
    from xml.etree import ElementTree as ET
    ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    with ZipFile(io.BytesIO(raw)) as archive:
        if archive.testzip() is not None:
            raise ValueError('DOCX archive integrity check failed')
        root = ET.fromstring(archive.read('word/document.xml'))
        # Word stores line breaks and tabs as elements rather than w:t text.
        # Include them when reconstructing content, including hyperlink runs.
        def paragraph_text(paragraph):
            parts = []
            for element in paragraph.iter():
                if element.tag == '{'+ns['w']+'}t':
                    parts.append(element.text or '')
                elif element.tag in ('{'+ns['w']+'}br', '{'+ns['w']+'}cr'):
                    parts.append('\n')
                elif element.tag == '{'+ns['w']+'}tab':
                    parts.append('\t')
            return ''.join(parts)
        paragraphs = [paragraph_text(p) for p in root.findall('.//w:p', ns)]
        expected = [header['display_name'], *header['contact_lines']]
        if header['links']:
            expected.append(' | '.join(link['label'] for link in header['links']))
        for section in draft['sections']:
            expected.extend([section['title'], *[f['text'] for f in section['facts']]])
        # python-docx maps each carriage return to a Word line break.
        expected = [text.replace('\r', '\n') for text in expected]
        if paragraphs != expected:
            raise ValueError('Rendered resume does not exactly preserve selected facts and header')
        rels = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
        external = [r.attrib['Target'] for r in rels if r.attrib.get('TargetMode')=='External']
        expected_urls={link['url'] for link in header['links']}
        for section in draft['sections']:
            for fact in section['facts']:
                expected_urls.update(match.group().rstrip('.,;)]}') for match in re.finditer(r'https?://[^\s<>]+',fact['text']))
        if set(external)!=expected_urls:
            raise ValueError('Rendered resume link targets differ from verified facts/header')
        styles=ET.fromstring(archive.read('word/styles.xml'))
        for style_id in ('Normal','Title','Heading1','ListBullet'):
            style=next(x for x in styles.findall('w:style',ns) if x.attrib.get('{'+ns['w']+'}styleId')==style_id)
            fonts=style.find('w:rPr/w:rFonts',ns)
            if fonts is None or fonts.attrib.get('{'+ns['w']+'}ascii')!='Times New Roman' or any('theme' in key.lower() for key in fonts.attrib):
                raise ValueError('Resume typography validation failed')
            if style.find('w:pPr/w:pBdr',ns) is not None:
                raise ValueError('Resume title/heading has an unexpected border')
    return {'status':'passed', 'scope':'docx_structure_exact_text_and_header_links',
            'page_count_verified':False, 'template_version':TEMPLATE_VERSION,
            'checks':['zip_integrity','exact_fact_text','header_text','all_link_targets','times_new_roman','no_title_borders']}


def save_artifact(draft, header, directory):
    header = validate_header(header)
    raw = render_docx(draft, header)
    validation = validate_docx(raw, draft, header)
    fingerprint = hashlib.sha256(raw).hexdigest()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory/(fingerprint+'.docx')
    # Publish immutable bytes first; a failed DB commit may leave an unreferenced file, never a ready event with a partial file.
    if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest()!=fingerprint:
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory,suffix='.tmp',delete=False) as file:
                name=file.name
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name,destination)
        finally:
            if name and Path(name).exists():
                Path(name).unlink()
    return {'artifact_id':fingerprint, 'path':str(destination), 'uri':'local://resumes/'+destination.name,
            'sha256':fingerprint, 'media_type':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'template_version':TEMPLATE_VERSION, 'validation':validation}


class Packager:
    def __init__(self, database, profile, version, artifact_dir='artifacts/resumes'):
        self.database, self.profile, self.version, self.artifact_dir = database, profile, version, artifact_dir
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.database,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO render_tasks(queue_order) SELECT queue_order FROM resume_drafts')
            db.execute("UPDATE render_tasks SET status='error',last_error='Render lease expired; retry budget exhausted' WHERE status='processing' AND lease_until<? AND attempts>=3",(now,))
            db.execute("UPDATE resume_queue SET status='resume_render_error' WHERE sequence IN (SELECT queue_order FROM render_tasks WHERE status='error' AND attempts>=3)")
            row=db.execute('''SELECT t.*, d.profile_version, d.result, q.job_id, q.run_id,
                q.snapshot, o.event_key FROM render_tasks t JOIN resume_drafts d ON d.queue_order=t.queue_order
                JOIN resume_queue q ON q.sequence=t.queue_order
                JOIN outbox o ON o.event_key='resume.drafted:'||q.job_id AND o.topic='resume.drafted.v1'
                WHERE t.attempts<3 AND ((t.status IN ('waiting','error') AND t.next_attempt_at<=?)
                OR (t.status='processing' AND t.lease_until<?))
                AND NOT EXISTS(SELECT 1 FROM consumer_receipts r WHERE r.consumer='resume-packager-v1' AND r.event_key=o.event_key)
                ORDER BY t.queue_order LIMIT 1''',(now,now)).fetchone()
            if not row:
                return None
            token=str(uuid.uuid4())
            db.execute("UPDATE render_tasks SET status='processing',attempts=attempts+1,worker_token=?,lease_until=? WHERE queue_order=?",(token,now+300,row['queue_order']))
            db.execute("UPDATE resume_queue SET status='resume_rendering' WHERE sequence=?",(row['queue_order'],))
            db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',(row['run_id'],'resume_rendering',f"#{row['queue_order']}: rendering and checking DOCX"))
            return {**dict(row),'worker_token':token}

    def process_one(self):
        task=self.claim()
        if not task:
            return False
        try:
            if task['profile_version']!=self.version:
                raise ValueError('Draft profile version differs from this worker profile; use the original verified profile')
            draft=json.loads(task['result'])
            facts={f['id']:f for f in self.profile['facts']}
            for section in draft['sections']:
                for fact in section['facts']:
                    if fact['id'] not in facts or facts[fact['id']]!=fact or fact['section']!=section['title']:
                        raise ValueError('Draft facts differ from the verified profile')
            artifact=save_artifact(draft,self.profile.get('_resume_header'),self.artifact_dir)
            event={'schema_version':1,'event_id':str(uuid.uuid4()),'correlation_id':draft.get('correlation_id',task['job_id']),
                   'job_id':task['job_id'],'profile_version':self.version,'job':json.loads(task['snapshot']),
                   'resume':{k:v for k,v in artifact.items() if k!='path'},
                   'unsupported_requirements':draft['missing_requirements'],'eligibility_status':'not_assessed',
                   'status':'ready_for_application'}
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                changed=db.execute("UPDATE render_tasks SET status='completed',worker_token=NULL,lease_until=NULL,last_error=NULL WHERE queue_order=? AND worker_token=?",(task['queue_order'],task['worker_token']))
                if changed.rowcount!=1:
                    raise ValueError('Render lease lost; stale artifact result discarded')
                db.execute('INSERT OR IGNORE INTO resume_artifacts(id,path,sha256,profile_version,template_version,validation) VALUES(?,?,?,?,?,?)',
                           (artifact['artifact_id'],artifact['path'],artifact['sha256'],self.version,TEMPLATE_VERSION,json.dumps(artifact['validation'])))
                cursor=db.execute('INSERT INTO application_queue(job_id,resume_artifact_id,payload) VALUES(?,?,?)',
                                  (task['job_id'],artifact['artifact_id'],json.dumps(event)))
                event['application_queue_order']=cursor.lastrowid
                db.execute('UPDATE application_queue SET payload=? WHERE sequence=?',(json.dumps(event),cursor.lastrowid))
                db.execute('INSERT INTO outbox(event_key,topic,payload) VALUES(?,?,?)',('applications.ready:'+task['job_id'],'applications.ready.v1',json.dumps(event)))
                db.execute('INSERT INTO consumer_receipts(consumer,event_key) VALUES(?,?)',('resume-packager-v1',task['event_key']))
                db.execute("UPDATE resume_queue SET status='resume_ready' WHERE sequence=?",(task['queue_order'],))
                db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',(task['run_id'],'resume_ready',f"#{task['queue_order']}: DOCX checked; queued for Agent 3 (not started)"))
        except Exception as exc:
            message=str(exc) if isinstance(exc,ValueError) else 'DOCX packaging failed; no application-ready event published'
            with self.connect() as db:
                changed=db.execute("UPDATE render_tasks SET status='error',last_error=?,worker_token=NULL,lease_until=NULL,next_attempt_at=? WHERE queue_order=? AND worker_token=?",
                                   (message,time.time()+30*(task['attempts']+1),task['queue_order'],task['worker_token']))
                if changed.rowcount:
                    db.execute("UPDATE resume_queue SET status='resume_render_error' WHERE sequence=?",(task['queue_order'],))
                    db.execute('INSERT INTO activity(run_id,kind,message) VALUES(?,?,?)',(task['run_id'],'resume_render_error',f"#{task['queue_order']}: {message}"))
        return True
