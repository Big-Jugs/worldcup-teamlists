"""PNG-SAM R2 18-person short-bench official final synthetic regression.
These are fabricated fixture/player scores. NO real fantasy scores are embedded.
The fixture-specific short-bench evidence comes from NRL's public team-list
report: https://www.nrl.com/news/2026/10/20/world-cup-team-lists-mens-round-2/
"""
import copy
from refresh_fantasy_scores import parse_payload, parse_embedded
from publish_scores_to_supabase import validate_feed, PublishError
import json, html


def sample():
    fixture={'id':'PNG-SAM','home':'PNG','away':'SAM','round':2}
    match={'matchState':'FullTime',
           'homeTeam':{'name':'Papua New Guinea','players':[]},
           'awayTeam':{'name':'Samoa','players':[]},
           'stats':{'players':{'homeTeam':[], 'awayTeam':[]}}}
    roster=[]
    for side, nation, count, eligible, base in [
            ('homeTeam','PNG',18,6,9000),('awayTeam','SAM',19,19,10000)]:
        for i in range(count):
            name=f'{nation} Mock {i+1}'
            pid=base+i
            match[side]['players'].append({'playerId':pid,'firstName':nation,'lastName':f'Mock {i+1}'})
            minutes=70 if i<17 else 0
            match['stats']['players'][side].append({'playerId':pid,'fantasyPointsTotal':50+i if minutes else 0,'minutesPlayed':minutes})
            if i<eligible:
                roster.append({'id':f'{nation}-{i}','name':name,'nation':nation,'marketEligible':True})
    # Market includes a seventh PNG player ruled OUT before kickoff, Luca Moretti.
    roster.append({'id':'PNG-UNSELECTED','name':'Luca Moretti','nation':'PNG','marketEligible':True})
    return {'match':match},fixture,roster


def feed(match,fixture):
    return {'schema':1,'rounds':{str(fixture['round']):{'matches':{fixture['id']:match}}}}


def test_verified_png_18_samoa_19_accepted():
    data,fixture,roster=sample()
    result,msg=parse_payload(data,fixture,roster)
    assert result and result['status']=='final',msg
    c=result['coverage']
    assert c['complete'] and c['official_teams']
    assert c['short_bench_exception']=='PNG-SAM-R2-PNG-18'
    assert (c['home']['rows'], c['home']['identified'],c['home']['appeared'],c['home']['unresolved'])==(18,18,17,0)
    assert (c['away']['rows'],c['away']['identified'],c['away']['appeared'],c['away']['unresolved'])==(19,19,17,0)
    assert len(c['home']['eligible_score_ids'])==6
    assert len(c['away']['eligible_score_ids'])==19
    assert 'PNG-UNSELECTED' not in result['players']
    assert validate_feed(feed(result,fixture))==1
    wrapped='<div q-data="'+html.escape(json.dumps(data),quote=True)+'"></div>'
    assert parse_embedded(wrapped,fixture,roster)[0]['coverage']['complete']


def reject_parse(data,fixture,roster):
    result,msg=parse_payload(data,fixture,roster)
    assert result is None,msg
    assert 'incomplete' in msg.lower(),msg


def reject_publish(result,fixture):
    try: validate_feed(feed(result,fixture))
    except PublishError: return
    raise AssertionError('Publisher accepted invalid short-bench final coverage')


def test_other_matches_cannot_use_short_bench_exception():
    for change in (
        {'round':3},
        {'id':'PNG-LEB','away':'LEB'},
        {'id':'ENG-FRA','home':'ENG','away':'FRA'},
        {'id':'AUS-FIJ','home':'AUS','away':'FIJ'},
    ):
        data,fixture,roster=sample()
        fixture.update(change)
        # Last names fixture and official nation must still match if we change
        # fixture sides. For round/id-only cases there is a valid nation map.
        reject_parse(data,fixture,roster)
    data,fixture,roster=sample()
    result,_=parse_payload(data,fixture,roster)
    for change in ({'round':3},{'id':'PNG-LEB'},{'home':'AUS'},{'away':'FIJ'}):
        f={**fixture,**change}
        m=copy.deepcopy(result)
        if 'home' in change: m['home']=change['home']
        if 'away' in change: m['away']=change['away']
        reject_publish(m,f)


def test_incomplete_png_table_rejected():
    data,fixture,roster=sample()
    home=data['match']['stats']['players']['homeTeam']
    home.pop()
    reject_parse(data,fixture,roster)
    data,fixture,roster=sample()
    home=data['match']['stats']['players']['homeTeam']
    home[16]['minutesPlayed']=0
    home[16]['fantasyPointsTotal']=0
    reject_parse(data,fixture,roster)  # 18 named but only 16 played
    data,fixture,roster=sample()
    data['match']['stats']['players']['homeTeam'][0].pop('fantasyPointsTotal')
    reject_parse(data,fixture,roster)  # missing played fantasy score
    data,fixture,roster=sample()
    data['match']['stats']['players']['homeTeam'][17].pop('minutesPlayed')
    reject_parse(data,fixture,roster)  # 0 score with unknown DNP
    data,fixture,roster=sample()
    data['match']['stats']['players']['homeTeam'][0]['playerId'] = 9001
    reject_parse(data,fixture,roster)  # duplicate official identity
    data,fixture,roster=sample()
    data['match']['stats']['players']['awayTeam'].pop()
    reject_parse(data,fixture,roster)  # Samoa still requires 19


def test_publisher_rejects_coverage_tampering():
    data,fixture,roster=sample()
    valid,_=parse_payload(data,fixture,roster)
    variations=[]
    no_mark=copy.deepcopy(valid);del no_mark['coverage']['short_bench_exception'];variations.append(no_mark)
    wrong_mark=copy.deepcopy(valid);wrong_mark['coverage']['short_bench_exception']='PNG-SAM-R3-PNG-18';variations.append(wrong_mark)
    partial=copy.deepcopy(valid);partial['coverage']['home']['rows']=17;variations.append(partial)
    fewer=copy.deepcopy(valid);fewer['coverage']['home']['appeared']=16;fewer['coverage']['home']['fantasy_appearances']=16;variations.append(fewer)
    extra=copy.deepcopy(valid);extra['players']['SPOOFED']={'played':True,'points':30};variations.append(extra)
    for m in variations: reject_publish(m,fixture)
    # A normal full 19-player side must remove this marker.
    data,fixture,roster=sample()
    home=data['match']['stats']['players']['homeTeam']
    home.append({'playerId':55555,'playerName':'PNG Mock Extra','fantasyPointsTotal':0,'minutesPlayed':0})
    result,msg=parse_payload(data,fixture,roster)
    assert result and result['coverage']['home']['rows']==19 and 'short_bench_exception' not in result['coverage'],msg
    assert validate_feed(feed(result,fixture))==1


if __name__=='__main__':
    for fn in (test_verified_png_18_samoa_19_accepted,
               test_other_matches_cannot_use_short_bench_exception,
               test_incomplete_png_table_rejected,
               test_publisher_rejects_coverage_tampering):
        fn()
        print('PASS',fn.__name__)
