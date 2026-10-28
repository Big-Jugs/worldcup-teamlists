"""Isolated deterministic verification; does NOT execute PL/pgSQL on Supabase."""
from __future__ import annotations
import html, json, importlib.util, os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent

def load(name):
    spec = importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

reader=load('refresh_fantasy_scores')
publisher=load('publish_scores_to_supabase')

def synthetic(*,status='final',players_per_side=17, zero_missing_minutes=False, correction=0, extra_dnp=False):
    roster=[]; fixture=dict(id='AUS-NZL',home='AUS',away='NZL',round_number=1)
    stats={}
    for side,nation in [('homeTeam','AUS'),('awayTeam','NZL')]:
        rows=[]
        for idx in range(players_per_side):
            ext_id=str(1000+(0 if nation=='AUS' else 100)+idx)
            name=f'{nation} Player {idx}'
            roster.append({'id':f'{nation}-{idx}', 'name':name, 'nation':nation,
                           'nrlId':ext_id,'marketEligible':True})
            pt=(0 if idx==0 else 20+idx)+correction
            row={'playerId':ext_id,'playerName':name,'fantasyPointsTotal':pt}
            if not (zero_missing_minutes and idx==0):row['minutesPlayed']=75
            rows.append(row)
        if extra_dnp:
            # Six-man named interchange: 17 active participants, 2 DNPs.
            for reserve_num in range(2):
                ext_id=str((2000 if nation=='AUS' else 3000)+reserve_num)
                name=f'{nation} Reserve {reserve_num+1}'
                suffix='dnp' if reserve_num==0 else 'dnp-extra'
                roster.append({'id':f'{nation}-{suffix}','name':name,'nation':nation,
                               'nrlId':ext_id,'marketEligible':True})
                rows.append({'playerId':ext_id,'playerName':name,
                             'fantasyPointsTotal':0,'minutesPlayed':0})
        stats[side]=rows
    match={'matchState':('fulltime' if status=='final' else 'inprogress'),
           'homeTeam':{'name':'Australia'},'awayTeam':{'name':'New Zealand'},
           'stats':{'players':stats}}
    return {'match':match},fixture,roster

def test_complete_finals():
    payload, fixture, roster=synthetic(extra_dnp=True)
    parsed,msg=reader.parse_payload(payload,fixture,roster)
    assert parsed and parsed['status']=='final',msg
    assert parsed['coverage']['complete']
    assert parsed['coverage']['home']['appeared']==17
    assert parsed['coverage']['away']['appeared']==17
    assert parsed['players']['AUS-dnp']=={'points':0,'played':False}
    assert parsed['players']['AUS-0']=={'points':0,'played':True}
    ids = parsed['coverage']['home']['eligible_score_ids'] + parsed['coverage']['away']['eligible_score_ids']
    assert set(ids) == set(parsed['players']) and len(ids) == len(set(ids))
    assert publisher.validate_feed({'schema':1,'rounds':{'1':{'matches':{'AUS-NZL':parsed}}}})==1
    return parsed


def test_reject_partial_score_map_even_with_valid_coverage():
    import copy
    valid = test_complete_finals()
    for variant in ('missing', 'extra', 'duplicate'):
        match = copy.deepcopy(valid)
        if variant == 'missing':
            del match['players']['AUS-1']
        elif variant == 'extra':
            match['players']['INVENTED'] = {'points': 55, 'played': True}
        elif variant == 'duplicate':
            match['coverage']['away']['eligible_score_ids'].append('NZL-2')
        elif variant == 'cross_side':
            match['coverage']['home']['eligible_score_ids'].append(
                match['coverage']['away']['eligible_score_ids'].pop())
        feed = {'schema':1,'rounds':{'1':{'matches':{'AUS-NZL':match}}}}
        try:
            publisher.validate_feed(feed)
        except publisher.PublishError:
            pass
        else:
            raise AssertionError(f'Unsafe final score/coverage mismatch accepted: {variant}')

def test_reject_partial_and_uncertain():
    payload, fixture, roster=synthetic(players_per_side=8)
    parsed,msg=reader.parse_payload(payload,fixture,roster)
    assert parsed is None and 'incomplete' in msg.lower(),msg
    payload, fixture, roster=synthetic(zero_missing_minutes=True,extra_dnp=True)
    parsed,msg=reader.parse_payload(payload,fixture,roster)
    assert parsed is None,msg
    payload, fixture, roster=synthetic(status='live',players_per_side=8)
    parsed,msg=reader.parse_payload(payload,fixture,roster)
    assert parsed and parsed['status']=='live',msg
    bad={'schema':1,'rounds':{'1':{'matches':{'AUS-NZL':{'home':'AUS','away':'NZL','status':'final','players':{}}}}}}
    try: publisher.validate_feed(bad)
    except publisher.PublishError:pass
    else:raise AssertionError('Uncovered finals must not publish')

def test_fresh_proofs_and_correction():
    match=test_complete_finals()
    p0,fx,roster=synthetic(extra_dnp=True)
    wrapped='<div id="vue-match-centre" q-data="'+html.escape(json.dumps(p0),quote=True)+'"></div>'
    from datetime import date
    fixture={'id':'AUS-NZL','round':1,'home':'AUS','away':'NZL',
             'date':'2026-10-15','url':'https://www.nrl.com/draw/rugby-league-world-cup/2026/round-1/australia-v-new-zealand/'}
    existing={'schema':1,'rounds':{}}
    proofs=[]
    with patch.object(reader,'datetime') as fake_dt:
        fake_dt.now.return_value=datetime(2026,10,15,21,0,tzinfo=timezone.utc)
        fake_dt.fromisoformat=datetime.fromisoformat
        out,n=reader.update({'fixtures':[fixture]},roster,existing,fetcher=lambda _: wrapped,
                            today=date(2026,10,15),source_proofs=proofs)
    assert n==1 and len(proofs)==1 and proofs[0]['coverage']['complete']
    # After a correction, the score changes but the proof still must come from
    # the freshly fetched page, not from the cached public feed.
    changed,fx,rs=synthetic(extra_dnp=True,correction=2)
    revised='<div q-data="'+html.escape(json.dumps(changed),quote=True)+'"></div>'
    proofs=[]
    updated,count=reader.update({'fixtures':[fixture]},roster,out,fetcher=lambda _: revised,
                                today=date(2026,10,15),source_proofs=proofs)
    assert count==1 and len(proofs)==1
    assert updated['rounds']['1']['matches']['AUS-NZL']['players']['AUS-1']['points']==23
    proofs=[]
    unchanged,count=reader.update({'fixtures':[fixture]},roster,updated,fetcher=lambda _: revised,
                                  today=date(2026,10,15),source_proofs=proofs)
    assert count==0 and len(proofs)==1


def test_coverage_only_correction_is_published():
    # An extra official bench DNP changes verification evidence, not player scores.
    # The updater must persist the new coverage so the DB attestation can match.
    import copy
    from datetime import date
    base,_,roster=synthetic(extra_dnp=True)
    updated=copy.deepcopy(base)
    updated['match']['stats']['players']['homeTeam'].append({
        'playerId':'999999','playerName':'Unmarketed reserve',
        'fantasyPointsTotal':0,'minutesPlayed':0})
    assert reader.parse_payload(base,{'home':'AUS','away':'NZL'},roster)[0]
    fixture={'id':'AUS-NZL','round':1,'home':'AUS','away':'NZL',
             'date':'2026-10-15', 'url':'https://www.nrl.com/draw/rugby-league-world-cup/2026/round-1/australia-v-new-zealand/'}
    def wrap(p):return '<div q-data="'+html.escape(json.dumps(p),quote=True)+'"></div>'
    feed={'schema':1,'rounds':{}}
    before,n1=reader.update({'fixtures':[fixture]},roster,feed,fetcher=lambda _:wrap(base),today=date(2026,10,15))
    after,n2=reader.update({'fixtures':[fixture]},roster,before,fetcher=lambda _:wrap(updated),today=date(2026,10,15))
    a=before['rounds']['1']['matches']['AUS-NZL']
    b=after['rounds']['1']['matches']['AUS-NZL']
    assert n1==1 and n2==1
    assert a['players']==b['players']
    assert a['coverage']!=b['coverage']

def test_publisher_sequence():
    match=test_complete_finals()
    feed={'schema':1,'rounds':{'1':{'matches':{'AUS-NZL':match}}}}
    requests=[]
    def fake_rpc(url,token,name,payload=None,opener=None):
        requests.append(name)
        return 0 if name=='wcf_lock_due_rounds' else 1 if name=='wcf_attest_recent_source' else {'success':True,'snapshots_checked':1} if name=='wcf_publish_scores' else {'success':True,'settled_rounds':[],'revised_player_prices':0}
    proof_path=ROOT/'wcf_source_proofs_runtime.json'
    proof_path.write_text(json.dumps({'proven':[{'fixture_id':'AUS-NZL','status':'final','players':match['players'],'coverage':match['coverage']}]}))
    try:
        with patch.object(publisher,'rpc',fake_rpc):
            publisher.publish('https://test.supabase.co','sb_secret_mock',feed)
    finally:proof_path.unlink(missing_ok=True)
    assert requests==['wcf_lock_due_rounds','wcf_publish_scores','wcf_attest_recent_source','wcf_settle_due_rounds'],requests

if __name__=='__main__':
    for fn in (test_complete_finals,test_reject_partial_score_map_even_with_valid_coverage,test_reject_partial_and_uncertain,
               test_coverage_only_correction_is_published,
               test_fresh_proofs_and_correction,test_publisher_sequence):
        fn()
        print('PASS',fn.__name__)
