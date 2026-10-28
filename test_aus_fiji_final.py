"""Regression tests using a SYNTHETIC 38-row Australia-Fiji final match.
This is not a snapshot of the NRL 25 October 2026 actual player scores.
"""
import copy, html, json
from refresh_fantasy_scores import (parse_payload, parse_embedded,
                                    safe_match_evidence, match_state)


def make_fixture():
    fixture={'id':'AUS-FIJ','home':'AUS','away':'FIJ','round':2}
    roster=[]
    stats={'homeTeam':[], 'awayTeam':[]}
    sides={'homeTeam':('AUS',80000), 'awayTeam':('FIJ',90000)}
    match={'matchState':'FullTime','homeTeam':{'name':'Australian Kangaroos', 'players':[]},
           'awayTeam':{'name':'Fiji Bati','players':[]}}
    for key,(nation,base) in sides.items():
        lineup=match[key]['players']
        for i in range(19):
            pid=base+i
            name=f'{nation} Sample {i+1}'
            # External IDs and names appear on official lineups separately
            # from stat records. Include some players not in the fantasy market.
            lineup.append({'firstName':nation,'lastName':f'Sample {i+1}',
                           'profileId':pid,'number':i+1})
            minutes=80 if i<13 else 25 if i<17 else 0
            score=42+i if minutes else 0
            stats[key].append({'profileId':pid,'fantasyPointsTotal':score,'minutesPlayed':minutes})
            if i<10:
                roster.append({'id':f'{nation}-{i}','name':name,'nation':nation,
                               'marketEligible':True})
    match['stats']={'players':stats}
    return {'match':match},fixture,roster


def test_full_final():
    data,fixture,roster=make_fixture()
    result,msg=parse_payload(data,fixture,roster)
    assert result and result['status']=='final',msg
    assert result['coverage']['complete']
    assert result['coverage']['official_teams']
    assert result['coverage']['home']['rows']==19
    assert result['coverage']['home']['appeared']==17
    assert result['coverage']['home']['unresolved']==0
    assert result['coverage']['away']['appeared']==17
    assert result['players']['AUS-0']=={'points':42,'played':True}
    for side in ('AUS','FIJ'):
        assert len([k for k in result['players'] if k.startswith(side+'-')])==10
    assert len(result['players'])==20
    wrapped='<div id="vue-match-centre" q-data="'+html.escape(json.dumps(data),quote=True)+'"></div>'
    reloaded,msg=parse_embedded(wrapped,fixture,roster)
    assert reloaded and len(reloaded['players'])==20,msg


def test_partial_final_rejected():
    data,fixture,roster=make_fixture()
    data['match']['stats']['players']['awayTeam'][8].pop('fantasyPointsTotal')
    result,msg=parse_payload(data,fixture,roster)
    assert result is None and 'incomplete' in msg.lower(),msg
    reason=safe_match_evidence(data,fixture,roster)
    assert 'unresolved=' in reason and 'fixture=AUS/FIJ' in reason,reason
    assert 'played=17' in reason,reason
    assert 'fantasy=16' in reason,reason


def test_wrong_match_rejected():
    data,fixture,roster=make_fixture()
    data['match']['awayTeam']['name']='England'
    parsed,msg=parse_payload(data,fixture,roster)
    assert parsed is None and 'attested' in msg.lower(),msg
    assert 'official teams=AUS/ENG' in safe_match_evidence(data,fixture,roster)


def test_live_not_final():
    data,fixture,roster=make_fixture()
    data['match']['matchState']='inprogress'
    parsed,msg=parse_payload(data,fixture,roster)
    assert parsed and parsed['status']=='live'
    data['match']['matchState']='NotStarted'
    parsed,msg=parse_payload(data,fixture,roster)
    assert parsed is None

if __name__=='__main__':
    for test in (test_full_final,test_partial_final_rejected,test_wrong_match_rejected,test_live_not_final):
        test()
        print('PASS',test.__name__)
