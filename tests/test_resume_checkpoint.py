import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.dashboard import Service
from agent.resume_checkpoint import generate_and_validate, inspect
from agent.resume_writer import request, validate, validate_draft
from test_resume_writer import fixture


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(str(Path(self.temp.name)/'agent.db'))
        self.profile,self.result = fixture()

    def tearDown(self):
        self.temp.cleanup()

    def run_draft(self, generator, task='test:one', profile=None):
        return generate_and_validate(self.service,task,{'description':'Java APIs'},
                                     profile or self.profile,'test-model',generator)

    def test_bullet_contract_and_sparse_evidence(self):
        schema=request({},self.profile,'test')['text']['format']['schema']
        bullets=schema['properties']['entries']['items']['properties']['bullets']
        self.assertEqual((bullets['minItems'],bullets['maxItems']),(1,5))
        for count in (0,1,2,5,6):
            result=copy.deepcopy(self.result)
            result['entries'][0]['bullets']=['Built Java APIs'+'.'*i for i in range(1,count+1)]
            if count in (0,6):
                with self.assertRaisesRegex(ValueError,rf"entries\[0\].bullets: source 'work'; expected 1–5 bullets, received {count}"):
                    validate(result,self.profile)
            else:
                self.assertTrue(validate(result,self.profile)['requires_review'])
        # Revalidate previously saved v1 drafts under their original rules.
        old=validate(self.result,self.profile,version='grounded-writer-v1')
        validate_draft(old,self.profile)

    def test_failed_response_survives_restart_without_paid_retry(self):
        self.result['entries'][0]['bullets']=[]
        generator=Mock(return_value=self.result)
        with self.assertRaisesRegex(ValueError,'saved checkpoint'):
            self.run_draft(generator)
        new_service=Service(self.service.database)
        with self.assertRaisesRegex(ValueError,'received 0'):
            generate_and_validate(new_service,'test:one',{'description':'Java APIs'},self.profile,'test-model',generator)
        self.assertEqual(generator.call_count,1)
        rows=inspect(self.service.database)['checkpoints']
        report=inspect(self.service.database,rows[0]['id'])
        self.assertEqual(report['entries'][0]['bullet_count'],0)
        self.assertEqual(report['current_validation_status'],'failed')
        self.assertNotIn('example@example.com',json.dumps(report))
        with self.service.connect() as db:
            self.assertNotIn('_resume_header',db.execute('SELECT profile FROM resume_generation_checkpoints').fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='resume_drafts'").fetchone()[0],0)

    def test_successful_response_replay_and_changed_input_isolation(self):
        generator=Mock(return_value=self.result)
        first=self.run_draft(generator)
        self.assertEqual(self.run_draft(generator),first)
        self.assertEqual(generator.call_count,1)
        self.run_draft(generator,task='test:new-request')
        profile=copy.deepcopy(self.profile)
        profile['facts'][-1]['text']='M.S. Computer Science, 2024'
        self.run_draft(generator,profile=profile)
        self.assertEqual(generator.call_count,3)
        self.assertEqual(len(inspect(self.service.database)['checkpoints']),3)

    def test_offline_inspection_never_calls_provider_or_changes_database(self):
        self.run_draft(Mock(return_value=self.result))
        before=Path(self.service.database).read_bytes()
        identifier=inspect(self.service.database)['checkpoints'][0]['id']
        with patch('agent.resume_worker.call_gpt',side_effect=AssertionError('Must not call GPT')):
            self.assertEqual(inspect(self.service.database,identifier)['current_validation_status'],'passed')
        self.assertEqual(before,Path(self.service.database).read_bytes())
        with self.assertRaisesRegex(ValueError,'Unknown checkpoint'):
            inspect(self.service.database,'missing')

    def test_profile_without_writable_evidence_fails_before_api(self):
        with self.assertRaisesRegex(ValueError,'Add verified experience'):
            request({}, {'profile_id':'empty','facts':[{'id':'s','section':'Skills','text':'Java'}]}, 'test')
