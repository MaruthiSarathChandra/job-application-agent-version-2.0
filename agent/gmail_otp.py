"""Restricted-behavior Gmail OTP reader for a future Agent 3 browser adapter.

OAuth uses Gmail's gmail.readonly scope (Google grants mailbox-wide *read*
permission for this scope). This module voluntarily restricts actual queries
to an explicit sender, dedicated label and requested-after timestamp; it never
sends, modifies or deletes email. No OTPs or message bodies are persisted.
"""
import argparse
import base64
from email.utils import parseaddr
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
DEFAULT_TOKEN_PATH = 'credentials/agent3-gmail-token.json'
LABEL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,60}$')
CODE_RE = re.compile(r'(?<!\d)\d{6}(?!\d)')
CONTEXT_RE = re.compile(r'\b(?:code|verify|verification|otp|one.time|security)\b', re.I)
MAX_AGE_SECONDS = 15 * 60


def validate_filter(sender, label, requested_at, now=None):
    now = time.time() if now is None else now
    if not isinstance(sender, str) or not re.fullmatch(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', sender):
        raise ValueError('Configure one exact OTP sender email address')
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
        raise ValueError('Configure one dedicated Gmail OTP label')
    if not isinstance(requested_at, (int, float)) or isinstance(requested_at, bool) or not now - MAX_AGE_SECONDS <= requested_at <= now + 30:
        raise ValueError('OTP request must be within the last 15 minutes')
    return sender.lower(), label, int(requested_at)


def extract_code(text):
    if not isinstance(text, str) or not CONTEXT_RE.search(text):
        return None
    matches = CODE_RE.findall(text)
    # An ambiguous message must never select a possibly unrelated number.
    return matches[0] if len(set(matches)) == 1 else None


def get_plain_text(payload):
    if not isinstance(payload, dict):
        return ''
    if payload.get('mimeType') == 'text/plain' and isinstance((payload.get('body') or {}).get('data'), str):
        encoded = payload['body']['data']
        try:
            return base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8', 'replace')
        except (ValueError, UnicodeError):
            return ''
    return '\n'.join(get_plain_text(part) for part in payload.get('parts', []))


def gmail_json(token, path, query=None, opener=urlopen):
    url = 'https://gmail.googleapis.com/gmail/v1/users/me/' + path
    if query:
        url += '?' + urlencode(query)
    request = Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    # Only GET requests; no email send, mark read, deletion, modification or watch.
    with opener(request, timeout=15) as response:
        raw = response.read(300_001)
    if len(raw) > 300_000:
        raise ValueError('Gmail response too large')
    return json.loads(raw)


def read_latest_otp(token, sender, label, requested_at, *, opener=urlopen, now=None):
    """Returns an OTP in memory; caller must avoid logging/persisting it."""
    sender, label, requested_at = validate_filter(sender, label, requested_at, now)
    if not isinstance(token, str) or not token.strip():
        raise ValueError('Gmail read-only OAuth token is missing')
    query = f'label:{label} from:{sender} after:{requested_at}'
    listings = gmail_json(token, 'messages', {'q': query, 'maxResults': 10}, opener=opener)
    if not isinstance(listings.get('messages', []), list):
        raise ValueError('Unexpected Gmail message list')
    for item in listings.get('messages', [])[:10]:
        identifier = item.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', identifier):
            continue
        message = gmail_json(token, 'messages/' + identifier, {'format':'full'}, opener=opener)
        try:
            timestamp = int(message.get('internalDate', 0)) / 1000
        except (ValueError, TypeError):
            continue
        if timestamp < requested_at or timestamp > (time.time() if now is None else now) + 30:
            continue
        headers = {h.get('name', '').casefold(): h.get('value','') for h in (message.get('payload') or {}).get('headers', [])
                   if isinstance(h, dict)}
        if parseaddr(headers.get('from', ''))[1].lower() != sender:
            continue
        code = extract_code(get_plain_text(message.get('payload')))
        if code:
            return code
    return None


def local_oauth_token(client_secrets, token_path=DEFAULT_TOKEN_PATH):
    """Local user consent; optional Google dependencies. Secrets stay outside Git."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleRequest
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise ValueError('Install optional Gmail dependencies: pip install -r requirements-gmail.txt') from None
    target = Path(token_path)
    credentials = None
    if target.exists():
        credentials = Credentials.from_authorized_user_file(str(target), [SCOPE])
        if set(credentials.scopes or []) != {SCOPE}:
            raise ValueError('Gmail token has unexpected scopes; revoke and reauthorize read-only access')
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleRequest())
    if not credentials or not credentials.valid:
        if not Path(client_secrets).is_file():
            raise ValueError('Create a Google OAuth Desktop client and store its JSON file locally')
        credentials = InstalledAppFlow.from_client_secrets_file(client_secrets, scopes=[SCOPE]).run_local_server(port=0)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Best effort private local file permissions; directory credentials/ is gitignored.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        file.write(credentials.to_json())
    return credentials.token


def main():
    parser = argparse.ArgumentParser(description='Read-only Gmail OTP diagnostics (no inbox modification)')
    parser.add_argument('--client-secrets', default='credentials/gmail-client.json')
    parser.add_argument('--token-file', default=DEFAULT_TOKEN_PATH)
    parser.add_argument('--connect', action='store_true', help='Authorize Gmail read-only; do not fetch email')
    parser.add_argument('--sender', help='Exact sender of an expected OTP')
    parser.add_argument('--label', default='JobAgentOTP')
    parser.add_argument('--requested-at', type=int, help='Unix time of current OTP request')
    args = parser.parse_args()
    try:
        if not args.connect and (not args.sender or not args.requested_at):
            raise ValueError('Specify --sender and --requested-at to check an expected OTP')
        if not args.connect:
            validate_filter(args.sender, args.label, args.requested_at)
        token = local_oauth_token(args.client_secrets, args.token_file)
        if args.connect:
            print('Gmail read-only authorization completed. No messages were read.')
        else:
            # Do not print raw OTP or sender to logs. Browser adapter can call
            # read_latest_otp in memory after it requests a verification code.
            available = read_latest_otp(token, args.sender, args.label, args.requested_at) is not None
            print(json.dumps({'otp_found': available, 'mail_modified': False}))
    except (ValueError, OSError) as exc:
        parser.exit(1, 'Gmail OTP error: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
