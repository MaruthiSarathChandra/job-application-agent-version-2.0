"""Deployment readiness is fail-closed, read-only and never logs credentials."""
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent.readiness import assess, main


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.db=self.root/'missing.db'
        self.profile=self.root/'profile.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_checkout_never_claims_production_readiness_or_creates_files(self):
        result=assess(str(self.db),str(self.profile),environ={})
        self.assertFalse(result['production_ready'])
        self.assertFalse(self.db.exists(), 'readiness must not create a database')
        self.assertFalse(self.profile.exists(), 'readiness must not create a profile')
        states={x['name']:x['passed'] for x in result['checks']}
        self.assertFalse(states['Employer-authorized submission adapter'])
        self.assertFalse(states['Live end-to-end receipt acceptance'])
        self.assertFalse(states['Deployment security'])
        self.assertFalse(states['Local database'])
        self.assertFalse(states['Resume model configuration'])

    def test_model_key_is_detected_but_never_printed(self):
        env={'OPENAI_API_KEY':'super-secret-local-key',
             'OPENAI_MODEL':'custom-model-name'}
        report=assess(str(self.db),str(self.profile),environ=env)
        self.assertTrue(next(x for x in report['checks']
                             if x['name']=='Resume model configuration')['passed'])
        encoded=json.dumps(report)
        self.assertNotIn('super-secret-local-key',encoded)
        self.assertNotIn('custom-model-name',encoded)
        self.assertFalse(report['production_ready'])

    def test_read_only_database_diagnostics_do_not_modify_bytes(self):
        db=sqlite3.connect(self.db)
        db.execute("CREATE TABLE jobs(id TEXT)")
        db.commit()
        db.close()
        before=self.db.read_bytes()
        result=assess(str(self.db),str(self.profile),environ={})
        self.assertFalse(next(c for c in result['checks']
                              if c['name']=='Local database')['passed'])
        self.assertEqual(before,self.db.read_bytes())

    def test_cli_has_nonzero_exit_even_when_model_env_is_present(self):
        output=io.StringIO()
        with patch.dict(os.environ,{'OPENAI_API_KEY':'secret-token',
                                    'OPENAI_MODEL':'model-name'},clear=True),redirect_stdout(output):
            code=main(['--database',str(self.db),'--profile',str(self.profile),
                       '--json'])
        self.assertEqual(code,2)
        data=json.loads(output.getvalue())
        self.assertFalse(data['production_ready'])
        self.assertNotIn('secret-token',output.getvalue())
        self.assertNotIn(str(self.root),output.getvalue())


if __name__ == '__main__':
    unittest.main()
