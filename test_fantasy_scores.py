"""Offline tests: match centre structure based on documented NRL match payload."""
import json,sys
from datetime import date
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from refresh_fantasy_scores import parse_payload,parse_embedded,update,normalise,active_fixture
roster=json.loads((ROOT/'roster.json').read_text())
fixture=json.loads((ROOT/'score_sources.json').read_text())['fixtures'][0]
aus=[p for p in roster if p['nation']=='AUS' and p['marketEligible']][:10]
nzl=[p for p in roster if p['nation']=='NZL' and p['marketEligible']][:10]
assert len(aus)==len(nzl)==10
# NRL documented structure: match.homeTeam.players holds identities,
# players.homeTeam[] holds stats by numeric NRL playerId, not names.
identity=lambda arr,off:[{'playerId':off+i,'firstName':p['name'].split()[0], 'lastName':' '.join(p['name'].split()[1:])} for i,p in enumerate(arr)]
stat=lambda arr,off:[{'playerId':off+i,'minutesPlayed':60 if i<9 else 0,'fantasyPointsTotal':i*4+10,'tries':0,'points':0} for i,p in enumerate(arr)]
raw={'data':{'match':{'matchId':'2026-match-1','matchState':'InProgress','matchMode':'Live',
                       'homeTeam':{'players':identity(aus,10000)},'awayTeam':{'players':identity(nzl,20000)}},
             'players':{'homeTeam':stat(aus,10000),'awayTeam':stat(nzl,20000)}}}
match,msg=parse_payload(raw,fixture,roster)
assert match and match['status']=='live' and len(match['players'])==20,(match,msg)
assert match['players'][aus[0]['id']]['points']==10
assert match['players'][nzl[1]['id']]['points']==14
assert not match['players'][aus[9]['id']]['played']
html='<script type="application/json">'+json.dumps(raw)+'</script>'
match,msg=parse_embedded(html,fixture,roster)
assert match and len(match['players'])==20,msg
# End of game, scores retained, live state removed.
raw['data']['match']['matchState']='FullTime'
fin,msg=parse_payload(raw,fixture,roster)
assert fin and fin['status']=='final' and len(fin['players'])==20
raw['data']['match']['matchState']='NotStarted'
assert parse_payload(raw,fixture,roster)[0] is None
raw['data']['match']['matchState']='InProgress'
# Scoreboards and season averages must NOT be substituted.
bad=json.loads(json.dumps(raw))
for side in ('homeTeam','awayTeam'):
 for p in bad['data']['players'][side]:p.pop('fantasyPointsTotal');p['average']=45
assert parse_payload(bad,fixture,roster)[0] is None
# Numeric ID row in global season data doesn't count without match state.
assert parse_payload({'players':{'homeTeam':stat(aus,10000),'awayTeam':stat(nzl,20000)}},fixture,roster)[0] is None
# Handle simple flat list of named player stats from alternate NRL API responses.
flat={'gameStatus':'In Progress','players':[{'playerName':p['name'],'fantasyPointsTotal':i+1} for i,p in enumerate(aus+nzl)]}
assert len(parse_payload(flat,fixture,roster)[0]['players'])==20
# Existing final cannot regress to a live match.
existing={'schema':1,'updatedAt':'2026-10-15T00:00:00Z','rounds':{'1':{'matches':{'AUS-NZL':fin}}}}
output,changes=update({'fixtures':[fixture]},roster,existing,fetcher=lambda u:html,today=date(2026,10,15))
assert changes==0 and output==existing
# Inactive future fixture should not be polled in today's job.
assert not active_fixture(fixture,{},date(2026,10,1))
assert active_fixture(fixture,{},date(2026,10,15))
print('PASS — match-scoped official Fantasy field, NRL ID mapping, final transition, excluded scoreboards, freshness and schedule')
