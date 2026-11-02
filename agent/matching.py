"""Explainable local skill overlap and explicit JD experience requirements.

This is not a probability, semantic eligibility verdict, or inferred user tenure.
"""
import re

SKILLS = {
 'Java':r'java', 'Python':r'python', 'JavaScript':r'javascript|\bjs',
 'TypeScript':r'typescript', 'C++':r'c\+\+', 'C#':r'c#', 'Go':r'golang|go programming',
 'Rust':r'rust', 'SQL':r'sql|mysql|postgresql|postgres|sql server',
 'MySQL':r'mysql', 'PostgreSQL':r'postgresql|postgres', 'MongoDB':r'mongodb',
 'Redis':r'redis', 'Spring Boot':r'spring\s*boot', 'Spring':r'spring',
 'Flask':r'flask', 'Django':r'django', 'FastAPI':r'fastapi',
 'React':r'react(?:js|\.js)?', 'Angular':r'angular', 'Node.js':r'node(?:\.js|js)',
 'HTML':r'html', 'CSS':r'css', 'REST':r'rest(?:ful)?(?:\s+apis?)?',
 'GraphQL':r'graphql', 'AWS':r'aws|amazon web services', 'Azure':r'azure',
 'GCP':r'gcp|google cloud', 'Docker':r'docker', 'Kubernetes':r'kubernetes|k8s',
 'Kafka':r'kafka', 'RabbitMQ':r'rabbitmq', 'Linux':r'linux', 'Git':r'git|github|gitlab',
 'CI/CD':r'ci/cd|continuous integration|continuous delivery',
 'Terraform':r'terraform', 'Jenkins':r'jenkins', 'JUnit':r'junit',
 'Postman':r'postman', 'Selenium':r'selenium', 'Pytest':r'pytest',
 'TensorFlow':r'tensorflow', 'PyTorch':r'pytorch', 'Pandas':r'pandas',
 'Spark':r'apache spark|pyspark', '.NET':r'\.net|dotnet', 'Ruby':r'ruby',
 'PHP':r'php', 'Swift':r'swift', 'Kotlin':r'kotlin', 'Scala':r'scala',
 'Elasticsearch':r'elasticsearch', 'DynamoDB':r'dynamodb', 'S3':r'amazon s3|aws s3|\bs3',
 'JWT':r'jwt|json web token', 'OAuth':r'oauth', 'gRPC':r'grpc',
}


def skills_in(text):
    return sorted(name for name,pattern in SKILLS.items()
                  if re.search(r'(?<![\w])(?:'+pattern+r')(?![\w])',text,re.I))


def profile_skills(profile):
    return skills_in(' '.join(f.get('text','') for f in profile.get('facts',[])))


def assess(job, rules):
    jd=skills_in(job.get('description',''))
    known=set(rules.get('profile_skills',[]))
    matched=[skill for skill in jd if skill in known]
    return {'method':'recognized_technical_skill_overlap_v1',
            'score':round(100*len(matched)/len(jd),2) if jd and known else None,
            'matched':matched,'missing':[skill for skill in jd if skill not in known],
            'total_jd_skills':len(jd),
            'note':'Recognized technical skills only; not eligibility or hiring probability'}


def required_years(description):
    # Preserve citations; do not count dates, company age, or preferred requirements.
    text=description.casefold().replace('’',"'")
    for number,word in enumerate(('zero','one','two','three','four','five','six','seven','eight','nine','ten')):
        text=re.sub(r'\b'+word+r'(?=\s+(?:or more\s+)?years?\b)',str(number),text)
    zero=re.search(r'no (?:prior |previous )?experience (?:is )?(?:required|necessary)|experience (?:is )?not required',text)
    values=[0] if zero else []; evidence=[zero.group(0)] if zero else []
    pattern=r'(?<!\d)(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s*\+?\s*(?:or more\s+)?years?\s*(?:\'\s*)?(?:of\s+)?(?:[a-z/-]+\s+){0,7}experience\b'
    for match in re.finditer(pattern,text):
        start=max(text.rfind('.',0,match.start()),text.rfind('\n',0,match.start()),text.rfind(';',0,match.start()))+1
        ends=[p for p in (text.find('.',match.end()),text.find('\n',match.end()),text.find(';',match.end())) if p!=-1]
        clause=text[start:min(ends) if ends else len(text)]
        if re.search(r'preferred|nice.to.have|a plus|desirable',clause):continue
        values.append(int(match.group(1)));evidence.append(match.group(0))
    return {'minimum':max(values) if values else None,'evidence':evidence,
            'method':'explicit_numeric_experience_requirements; maximum across recognized requirements'}


def annotate(job,rules):
    return {**job,'match_evidence':assess(job,rules),
            'experience_requirement':required_years(job.get('description',''))}


def filter_reasons(job,rules):
    reasons=[]
    ceiling=rules.get('max_required_years')
    if ceiling is not None:
        years=required_years(job.get('description',''))['minimum']
        if years is None:reasons.append('JD required years unknown; cannot verify selected experience ceiling')
        elif years>ceiling:reasons.append(f'JD requires at least {years} years; selected maximum is {ceiling}')
    threshold=rules.get('min_match_percent',0)
    if threshold:
        score=assess(job,rules)['score']
        if score is None:reasons.append('Technical-skill match is unknown; cannot verify selected threshold')
        elif score<threshold:reasons.append(f'Technical-skill match {score}% is below {threshold}%')
    return reasons
