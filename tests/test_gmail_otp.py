"""Gmail OTP policy tests use mocked read-only HTTP; no mailbox connection."""
import base64
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from agent.gmail_otp import extract_code, read_latest_otp, validate_filter


class FakeReply:
    def __init__(self, payload):
        self.content = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return self.content[:limit]


class GmailOTPTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.since = self.now - 120
        self.sender = 'otp@employer.example'
        self.label = 'JobAgentOTP'
        self.calls = []

    def reader(self, sender=None, when=None, body=None):
        sender = sender or self.sender
        when = when if when is not None else self.now - 30
        body = body or 'Your verification code is 462819.'
        def fake(request, timeout=15):
            self.calls.append(request)
            assert request.get_method() == 'GET'
            assert request.get_header('Authorization') == 'Bearer example-token'
            if urlsplit(request.full_url).path.endswith('/messages'):
                return FakeReply({'messages':[{'id':'a1'}]})
            return FakeReply({'id':'a1', 'internalDate':str(when*1000),
                'payload':{'headers':[{'name':'From','value':sender}],
                    'mimeType':'text/plain',
                    'body':{'data':base64.urlsafe_b64encode(body.encode()).decode().rstrip('=')}}})
        return fake

    def test_only_expected_sender_label_and_fresh_mail(self):
        code=read_latest_otp('example-token',self.sender,self.label,self.since,
                             opener=self.reader(),now=self.now)
        self.assertEqual(code,'462819')
        self.assertEqual(len(self.calls),2)
        params=parse_qs(urlsplit(self.calls[0].full_url).query)
        self.assertIn('label:JobAgentOTP',params['q'][0])
        self.assertIn('from:otp@employer.example',params['q'][0])
        self.assertIn('after:'+str(self.since),params['q'][0])
        self.assertEqual(params['maxResults'],['10'])

    def test_sender_spoofed_in_from_is_rejected(self):
        code=read_latest_otp('example-token',self.sender,self.label,self.since,
                             opener=self.reader(sender='attacker@other.example'),now=self.now)
        self.assertIsNone(code)

    def test_old_message_is_rejected(self):
        code=read_latest_otp('example-token',self.sender,self.label,self.since,
                             opener=self.reader(when=self.since-20),now=self.now)
        self.assertIsNone(code)

    def test_ambiguous_or_non_otp_text_is_rejected(self):
        self.assertIsNone(extract_code('Order 123456 is ready'))
        self.assertIsNone(extract_code('Verification code 123456 or 654321'))
        self.assertEqual(extract_code('One-time code: 123456'),'123456')

    def test_validation_rejects_unbounded_mailbox_search(self):
        for sender,label,after in (
            ('',self.label,self.since),('person@gmail.com OR all',self.label,self.since),
            (self.sender,'in:anywhere',self.since),(self.sender,self.label,self.now-5000),
            (self.sender,self.label,self.now+600)):
            with self.subTest(sender=sender,label=label,after=after):
                with self.assertRaises(ValueError):
                    validate_filter(sender,label,after,now=self.now)


if __name__ == '__main__':
    unittest.main()
