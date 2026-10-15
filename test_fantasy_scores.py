"""Replacement for scoring repository's historical 10-player final test.

The old test certified an incomplete final as valid. This version retains
provisional-parse checks and requires full verified final-match coverage.
"""
import json,html
from refresh_fantasy_scores import parse_payload, parse_embedded, fantasy_of
from test_hardening import (test_complete_finals,test_reject_partial_score_map_even_with_valid_coverage,test_reject_partial_and_uncertain,
                           test_fresh_proofs_and_correction,test_publisher_sequence,
                           test_coverage_only_correction_is_published,
                           synthetic)

payload,fixture,roster=synthetic(status='live',players_per_side=17,extra_dnp=True)
match,msg=parse_payload(payload,fixture,roster)
assert match and match['status']=='live',(match,msg)
assert match['players']['AUS-0']=={'points':0,'played':True}
assert match['players']['NZL-dnp']=={'points':0,'played':False}
assert len(match['players'])==36
html_source='<div q-data="'+html.escape(json.dumps(payload),quote=True)+'"></div>'
parsed,msg=parse_embedded(html_source,fixture,roster)
assert parsed and len(parsed['players'])==36,msg
payload['match']['matchState']='NotStarted'
assert parse_payload(payload,fixture,roster)[0] is None
payload['match']['matchState']='inprogress'
partial=json.loads(json.dumps(payload))
for side in ('homeTeam','awayTeam'):
    for person in partial['match']['stats']['players'][side]:
        person.pop('fantasyPointsTotal',None)
        person['average']=50
assert parse_payload(partial,fixture,roster)[0] is None
assert fantasy_of({'average':50}) is None
for fn in (test_complete_finals,test_reject_partial_score_map_even_with_valid_coverage,test_reject_partial_and_uncertain,
           test_fresh_proofs_and_correction,test_publisher_sequence,
           test_coverage_only_correction_is_published):
    fn()
print('PASS source identities, live points, DNP, incomplete-final rejection, revisions and publisher order')
