"""Safe browser prefill tests; do not start a real browser."""
import json
import tempfile
import unittest
from pathlib import Path

from agent.application_browser import fill_visible_fields, verified_answers


class FakeLocator:
    def __init__(self, selector, fields, filled):
        self.selector, self.fields, self.filled = selector,fields,filled
    def count(self):
        return 1 if self.selector in self.fields else 0
    @property
    def first(self):
        return self
    def is_visible(self):
        return True
    def is_enabled(self):
        return True
    def fill(self, value):
        self.filled.append((self.selector,value))
    def get_attribute(self, key):
        return self.fields[self.selector].get(key)
    def set_input_files(self, path):
        self.filled.append(('resume_file',path))


class FakePage:
    def __init__(self, fields):
        self.fields=fields
        self.filled=[]
    def locator(self, selector):
        return FakeLocator(selector,self.fields,self.filled)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'answers.json'

    def tearDown(self):
        self.temp.cleanup()

    def answers(self, **changes):
        value={'verified':True,'first_name':'Verified','last_name':'Candidate',
               'email':'candidate@example.com'}
        value.update(changes)
        self.path.write_text(json.dumps(value))
        return verified_answers(self.path)

    def test_only_verified_answers_are_consumed(self):
        with self.assertRaises(ValueError):
            self.answers(verified=False)
        with self.assertRaises(ValueError):
            self.answers(social_security_number='111223333')
        with self.assertRaises(ValueError):
            self.answers(email='not-an-email')
        result=self.answers(linkedin='https://www.linkedin.com/in/verified/')
        self.assertEqual(set(result),{'first_name','last_name','email','linkedin'})
        self.assertNotIn('verified',result)

    def test_prefill_never_submits_or_answers_unrecognized_questions(self):
        fields={'input[autocomplete="given-name"]':{},'input[type="email"]':{},
                'input[type="file"]':{'accept':'.docx,.pdf'},'button[type="submit"]':{}}
        page=FakePage(fields)
        result=fill_visible_fields(page,self.answers(),Path('/path/approved.docx'))
        self.assertEqual(set(result['filled_fields']),{'first_name','email'})
        self.assertEqual(result['unmapped_fields'],['last_name'])
        self.assertTrue(result['resume_uploaded'])
        self.assertFalse(result['submitted'])
        self.assertEqual(len(page.filled),3)
        self.assertNotIn('button[type="submit"]',[x[0] for x in page.filled])

    def test_pdf_only_upload_is_not_forced(self):
        page=FakePage({'input[type="file"]':{'accept':'.pdf'}})
        result=fill_visible_fields(page,self.answers(),Path('/path/approved.docx'))
        self.assertFalse(result['resume_uploaded'])
        self.assertEqual(page.filled,[])


if __name__ == '__main__':
    unittest.main()
