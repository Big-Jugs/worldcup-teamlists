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


def test_actual_aus16_fij17_complete_with_six_fiji_market_players():
    # Reproduces observed 28 Oct source: 19 rows per side, 16/17 played,
    # no unresolved, but only 6 eligible Fiji market players. All values
    # in this test are synthetic; these are NOT actual fantasy point totals.
    from publish_scores_to_supabase import validate_feed
    data,fixture,roster=make_fixture()
    aussie_row=data['match']['stats']['players']['homeTeam'][16]
    aussie_row['minutesPlayed']=0
    aussie_row['fantasyPointsTotal']=0
    for i in range(10,19):
        roster.append({'id':f'AUS-{i}','name':f'AUS Sample {i+1}',
                       'nation':'AUS','marketEligible':True})
    for person in roster:
        if person['nation']=='FIJ' and int(person['id'].split('-')[-1])>=6:
            person['marketEligible']=False
    parsed,msg=parse_payload(data,fixture,roster)
    assert parsed is not None and parsed['status']=='final',msg
    cover=parsed['coverage']
    assert cover['complete'] is True and cover['official_teams'] is True
    assert (cover['home']['rows'],cover['home']['identified'],cover['home']['appeared'],cover['home']['unresolved'])==(19,19,16,0)
    assert (cover['away']['rows'],cover['away']['identified'],cover['away']['appeared'],cover['away']['unresolved'])==(19,19,17,0)
    assert len(cover['home']['eligible_score_ids'])==19
    assert len(cover['away']['eligible_score_ids'])==6
    assert parsed['players']['AUS-16']=={'points':0,'played':False}
    assert validate_feed({'schema':1,'rounds':{'2':{'matches':{'AUS-FIJ':parsed}}}})==1


def test_reject_missing_named_bench_row_even_with_sixteen_appearances():
    data,fixture,roster=make_fixture()
    data['match']['stats']['players']['homeTeam'][16]['minutesPlayed']=0
    data['match']['stats']['players']['homeTeam'][16]['fantasyPointsTotal']=0
    data['match']['stats']['players']['homeTeam'].pop()
    parsed,msg=parse_payload(data,fixture,roster)
    assert parsed is None and 'incomplete' in msg.lower(),msg


def test_reject_uncertain_dnp_and_missing_played_points():
    data,fixture,roster=make_fixture()
    side=data['match']['stats']['players']['homeTeam']
    side[16]['fantasyPointsTotal']=0
    side[16].pop('minutesPlayed')  # ambiguous: real DNP or played 0 points?
    parsed,msg=parse_payload(data,fixture,roster)
    assert parsed is None and 'incomplete' in msg.lower(),msg
    data,fixture,roster=make_fixture()
    data['match']['stats']['players']['awayTeam'][0].pop('fantasyPointsTotal')
    parsed,msg=parse_payload(data,fixture,roster)
    assert parsed is None and 'incomplete' in msg.lower(),msg


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
    for test in (test_full_final,test_actual_aus16_fij17_complete_with_six_fiji_market_players,test_reject_missing_named_bench_row_even_with_sixteen_appearances,test_reject_uncertain_dnp_and_missing_played_points,test_partial_final_rejected,test_wrong_match_rejected,test_live_not_final):
        test()
        print('PASS',test.__name__)
