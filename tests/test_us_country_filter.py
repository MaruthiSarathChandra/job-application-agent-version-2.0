"""Country/location safety regressions; no live sites or credentials."""
import json
import tempfile
import unittest
from pathlib import Path

from agent.dashboard import Service, rules_from_request
from agent.intake import qualify, country_from_job, matches_location


class CountryFilterTests(unittest.TestCase):
    def test_kansas_city_detailed_address_matches_requested_city(self):
        job={'title':'Java Developer','company':'Company','location':'Kansas City, MO, US',
             'description':'Develop Java and Spring Boot APIs','country_code':'US'}
        rules={'title_keywords':['Java Developer'],'locations':['Kansas City'],
               'country_codes':['US'],'jd_keywords':['Java']}
        self.assertEqual(qualify(job,rules)['status'],'matched')
        self.assertFalse(matches_location('Arkansas City, KS','Kansas City'))
        self.assertFalse(matches_location('Kansas City, KS','Kansas City, MO'))

    def test_argentina_and_unknown_country_are_not_queued_with_us_filter(self):
        base={'title':'Software Engineer','description':'Java services'}
        for location in ('Buenos Aires, Argentina','Argentina','Remote','Hyderabad, India'):
            with self.subTest(location=location):
                job={**base,'location':location}
                result=qualify(job,{'title_keywords':['Software Engineer'],
                                    'locations':[],'country_codes':['US']})
                self.assertEqual(result['status'],'excluded')
                self.assertIn('Country outside configured countries or not verifiable from posting',
                              result['reasons'])
        for location in ('Kansas City, MO','New York, NY','Remote US','San Francisco, California'):
            self.assertEqual(country_from_job({'location':location}),'US')

    def test_iso_countries_input_is_explicit_and_normalized(self):
        request={'prompt':'Find Software Engineer jobs','sources':['directory:verified'],
                 'country_codes':['us','US'], 'locations':['Kansas City']}
        _,rules,sources=rules_from_request(request)
        self.assertEqual(rules['country_codes'],['US'])
        self.assertEqual(sources,['directory:verified'])
        with self.assertRaises(ValueError):
            rules_from_request({**request,'country_codes':['usa']})

    def test_real_pipeline_prevents_foreign_location_enqueuing(self):
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'jobs.db')
            jobs=[
                {'url':'https://example.com/jobs/a','title':'Software Engineer','company':'Example',
                 'location':'Buenos Aires, Argentina','description':'Java APIs',
                 'employer_key':'fixture','requisition_id':'a'},
                {'url':'https://example.com/jobs/b','title':'Software Engineer','company':'Example',
                 'location':'Kansas City, MO, United States','description':'Java APIs',
                 'employer_key':'fixture','requisition_id':'b'}]
            service=Service(db,fetcher=lambda _:jobs)
            service.start({'prompt':'Software Engineer','sources':['greenhouse:fixture'],
                           'country_codes':['US']},background=False)
            state=service.snapshot()
            self.assertEqual(len(state['queue']),1)
            self.assertEqual(len(state['jobs']),2)
            self.assertEqual({j['qualification']['status'] for j in state['jobs']},
                             {'excluded','matched'})


if __name__=='__main__':
    unittest.main()
