#!/usr/bin/env python3
"""Read match-scoped NRL.com *fantasyPointsTotal* into World Cup Fantasy.

2026 World Cup feed: best effort; live page/API shape must be checked at kickoff.
Official fixture URLs are the only remote locations. Never infer fantasy points
from a match score, a seasonal average, or other non-match data.
"""
from __future__ import annotations
import argparse, copy, html, json, os, re, sys, unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "Mozilla/5.0 (WorldCupFantasy fan prototype; NRL match stat verification)"}
FANTASY_KEYS = ('fantasyPointsTotal','fantasyPoints','fantasy_points','fantasyScore')
NAME_KEYS = ('playerName','fullName','name','displayName','player_name')
ID_KEYS = ('playerId','player_id','personId')
STATES = ('matchState','gameState','gameStatus','matchStatus','status','matchMode')


def normalise(value):
    t = ''.join(c for c in unicodedata.normalize('NFKD', html.unescape(str(value))) if not unicodedata.combining(c)).lower()
    t = re.sub(r'[^a-z0-9]', '', t)
    return {'sualauvifaalogo':'suafaalogo','suaafaalogo':'suafaalogo','kennethlio':'kliro'}.get(t,t)


def state_of(value):
    v = re.sub(r'[^a-z0-9]', '', str(value).lower())
    if v in ('post', 'postmatch','fulltime','complete','completed','finished','final','ft','aftergame'):return 'final'
    if v in ('live','inprogress','playing','firsthalf','secondhalf','1sthalf','2ndhalf','halftime','half time','extratime','extrahalf','overtime','onfield','inplay','running'):return 'live'
    return None


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values(): yield from walk(v)
    elif isinstance(node, list):
        for x in node: yield from walk(x)


def match_state(obj):
    # A not-started matchState overrides a stale or generic matchMode.
    if not isinstance(obj,dict):return None
    primary=obj.get('matchState',obj.get('gameState',obj.get('gameStatus')))
    if primary is not None and re.sub(r'[^a-z0-9]','',str(primary).lower()) in ('notstarted','prematch','pre','upcoming','scheduled','notyetstarted'):
        return None
    # Reject aggregate values from unrelated historical fixtures.
    for key in STATES:
        if key in obj:
            s = state_of(obj[key])
            if s: return s
    return None


def identify(obj):
    if not isinstance(obj,dict):return None
    for k in NAME_KEYS:
        if isinstance(obj.get(k),str) and obj[k].strip():return obj[k]
    if isinstance(obj.get('firstName'),str) and isinstance(obj.get('lastName'),str):
        return obj['firstName']+' '+obj['lastName']
    if isinstance(obj.get('player'),dict): return identify(obj['player'])
    return None


def id_of(obj):
    for k in ID_KEYS:
        if obj.get(k) is not None:return str(obj[k])
    if isinstance(obj.get('player'),dict):return id_of(obj['player'])
    return None


def fantasy_of(obj):
    if not isinstance(obj, dict):return None
    for layer in (obj, obj.get('playerStats'), obj.get('stats'), obj.get('statistics')):
        if not isinstance(layer,dict):continue
        for key in FANTASY_KEYS:
            if key in layer:
                raw=layer[key]
                if isinstance(raw,bool):return None
                try:num=float(raw)
                except (TypeError,ValueError):return None
                if num!=int(num) or not -60<=num<=250:return None
                return int(num)
    return None


def extract_embedded(html_text):
    soup=BeautifulSoup(html_text,'html.parser')
    # Modern NRL match-centre pages are Quasar/Vue. The official match
    # object, including match-player statistics, may be serialised as an
    # HTML-escaped JSON attribute rather than a script or network response.
    # This also runs against the Playwright-rendered HTML fallback.
    for node in soup.select('#vue-match-centre[q-data], [q-data]'):
        raw=node.get('q-data')
        if not raw:
            continue
        for candidate in (raw, html.unescape(raw)):
            try:
                parsed=json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed,(dict,list)):
                yield parsed
                break
    for script in soup.find_all('script'):
        body=script.string or script.get_text() or ''
        if not body.strip():continue
        if script.get('type') in ('application/json','application/ld+json') or script.get('id')=='__NEXT_DATA__':
            try: yield json.loads(body)
            except (ValueError,TypeError): pass
        for m in re.finditer(r'(?:window\.__INITIAL_STATE__|window\.__PRELOADED_STATE__|window\.__NEXT_DATA__|window\.__NUXT__)\s*=\s*',body):
            try:yield json.JSONDecoder().raw_decode(body[m.end():])[0]
            except (ValueError,TypeError):pass
    # HTML-escaped JSON snippets inside component demo pages or attributes.
    # Only when it has a match object; do not interpret general page-wide text.
    for node in soup.select('[data-match-json], [data-match-state]'):
        raw=node.get('data-match-json')
        if raw:
            try:yield json.loads(html.unescape(raw))
            except (ValueError,TypeError):pass


def roster_lookups(roster, fixture):
    target={fixture['home'],fixture['away']}
    by_name={}; by_id={}; eligible_ids=set()
    for p in roster:
        if not p.get('marketEligible') or p.get('nation') not in target:continue
        eligible_ids.add(p['id'])
        by_name[(p['nation'],normalise(p['name']))]=p
        if p.get('nrlId'): by_id[str(p['nrlId'])]=p
    return by_name,by_id,eligible_ids


def names_by_external_id(root):
    # NRL match-centre format stores stats separately from match-home/away
    # lineups. For example: match.homeTeam.players vs players.homeTeam[].
    found={}
    for node in walk(root):
        pid=id_of(node)
        name=identify(node)
        if pid and name and 'fantasyPointsTotal' not in node:
            if pid not in found:found[pid]=name
    return found


def appearance_of(row, points):
    """Never interpret a zero point row with no minutes as a known appearance.

    A nonzero official fantasy score is sufficient evidence of appearance. A
    reported 0 minutes explicitly indicates DNP even if a row is present.
    """
    layers=(row, row.get('playerStats'), row.get('stats'), row.get('statistics'))
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for key in ('minutesPlayed','mins','minutes','timeOnField'):
            if layer.get(key) is not None:
                try:
                    return float(layer[key])>0
                except (ValueError,TypeError):
                    return None
    return True if points is not None and points != 0 else None


def player_for_row(row, side, lookup_name, lookup_id, id_to_name):
    pid=id_of(row)
    nm=identify(row) or (id_to_name.get(pid) if pid else None)
    person=lookup_name.get((side,normalise(nm))) if nm else None
    if not person and pid:
        candidate=lookup_id.get(pid)
        if candidate and candidate['nation']==side:
            person=candidate
    return person, bool(pid or nm)


def scored_rows(rows, side, lookup_name, lookup_id, id_to_name):
    out={}
    if not isinstance(rows,list):return out
    for row in rows:
        if not isinstance(row,dict):continue
        pts=fantasy_of(row)
        if pts is None:continue
        person,_=player_for_row(row,side,lookup_name,lookup_id,id_to_name)
        if not person:continue
        played=appearance_of(row,pts)
        # This player has no verified appearance result yet. Never guess DNP.
        if played is None:continue
        out[person['id']]={'points':pts,'played':played}
    return out


def official_side_coverage(rows,side,lookup_name,lookup_id,id_to_name):
    """Evidence of a complete official match-player table, not merely 8 hits.

    Rows for unselectable players still count toward 17 real participants.
    An unidentifiable official participant, a duplicate identity, or a played
    player without fantasy/minutes evidence blocks final settlement.
    """
    identities=set(); appeared=0; scored=0; unresolved=0; eligible_score_ids=[]
    for row in rows:
        if not isinstance(row,dict):
            unresolved+=1
            continue
        points=fantasy_of(row)
        player,known_identity=player_for_row(row,side,lookup_name,lookup_id,id_to_name)
        identity=id_of(row) or normalise(identify(row) or '')
        if not known_identity or not identity or identity in identities:
            unresolved+=1
            continue
        identities.add(identity)
        played=appearance_of(row,points)
        if played is None:
            unresolved+=1
        elif played:
            appeared+=1
            if points is None:
                unresolved+=1
            else:
                scored+=1
        if player and played is not None and points is not None:
            eligible_score_ids.append(player['id'])
        # A matched game player with missing fantasy evidence can never be
        # assigned an invented 0 score, regardless of prior averages.
        if player and played is None:
            unresolved+=1
    return {'rows':len(rows),'identified':len(identities),'appeared':appeared,
            'fantasy_appearances':scored,'unresolved':unresolved,
            'eligible_score_ids':sorted(eligible_score_ids)}


def official_match_coverage(stats,participants,by_name,by_id,id_to_name):
    if not isinstance(stats,dict) or not all(isinstance(stats.get(side),list) for side in ('homeTeam','awayTeam')):
        return None
    h=official_side_coverage(stats['homeTeam'],participants['home'],by_name,by_id,id_to_name)
    a=official_side_coverage(stats['awayTeam'],participants['away'],by_name,by_id,id_to_name)
    # Seventeen played participants on each side is a conservative rugby
    # league baseline. Additional 2026 interchange players are supported.
    complete=all(x['appeared']>=17 and x['fantasy_appearances']==x['appeared']
                 and x['unresolved']==0 for x in (h,a))
    return {'home':h,'away':a,'complete':complete,
            'home_code':participants['home'],'away_code':participants['away']}



# World Cup semi-finalists are unknown until the knockout draw is finalised.
# Resolve both sides ONLY using the current NRL match's own named teams.
# Unknown/TBC match sides never receive guessed scores or mislabelled players.
NATION_NAMES = {
    'australia': 'AUS', 'kangaroos': 'AUS', 'aus': 'AUS',
    'newzealand': 'NZL', 'kiwis': 'NZL', 'nzl': 'NZL',
    'england': 'ENG', 'eng': 'ENG',
    'samoa': 'SAM', 'toasamoa': 'SAM', 'sam': 'SAM',
    'tonga': 'TON', 'matemaatonga': 'TON', 'ton': 'TON',
    'papuanewguinea': 'PNG', 'pngkumuls': 'PNG', 'kumuls': 'PNG', 'png': 'PNG',
    'fiji': 'FIJ', 'fijibati': 'FIJ', 'fij': 'FIJ',
    'cookislands': 'COK', 'cookislandsakukura': 'COK', 'cok': 'COK', 'coo': 'COK',
    'france': 'FRA', 'fra': 'FRA', 'lebanon': 'LEB', 'cedars': 'LEB', 'leb': 'LEB'
}


def nation_from_team(team):
    if isinstance(team, str):
        return NATION_NAMES.get(normalise(team))
    if isinstance(team, dict):
        # Match team identity only, not individual players or unrelated stats.
        for field in ('name', 'teamName', 'displayName', 'shortName',
                      'abbreviation', 'code', 'teamCode', 'teamShortName', 'fullName',
                      'nickName', 'nickname',
                      'countryName', 'nation'):
            v = team.get(field)
            if isinstance(v, str):
                n = NATION_NAMES.get(normalise(v))
                if n:
                    return n
        for field in ('team', 'teamInfo', 'details'):
            value = team.get(field)
            if isinstance(value, dict):
                n = nation_from_team(value)
                if n:
                    return n
    return None


def resolved_fixture(match, fixture):
    home, away = fixture['home'], fixture['away']
    if home != 'TBC' and away != 'TBC':
        return fixture
    # Avoid accidentally accepting teams from a historical child match.
    if not isinstance(match, dict):
        return None
    official_home = nation_from_team(match.get('homeTeam'))
    official_away = nation_from_team(match.get('awayTeam'))
    if official_home is None or official_away is None or official_home == official_away:
        return None
    if home != 'TBC' and home != official_home:
        return None
    if away != 'TBC' and away != official_away:
        return None
    return dict(fixture, home=official_home, away=official_away)


def current_match_containers(payload):
    """Walk page-state containers, not unrelated historical/fixture matches.

    NRL q-data may bundle an upcoming current match AND completed recent matches.
    Never publish a historical match as a live World Cup fixture merely because
    its data also contains a valid Fantasy table and two recognisable teams.
    """
    excluded={'stats','statistics','history','recentmatches','previousmatches',
              'matchhistory','headtohead','fixtures','matches','results',
              'players','playerstats','hometeam','awayteam','teams',
              'standings','table','schedule','games'}
    def visit(obj, depth=0):
        if depth>16:
            return
        if isinstance(obj, dict):
            yield obj
            for key,value in obj.items():
                k=re.sub(r'[^a-z0-9]', '', str(key).lower())
                if k in excluded:
                    continue
                if isinstance(value, (dict,list)):
                    yield from visit(value,depth+1)
        elif isinstance(obj,list):
            for value in obj:
                yield from visit(value,depth+1)
    yield from visit(payload)


def parse_payload(payload, fixture, roster, *, probe=False):
    """Parse NRL match-centre stats using the CURRENT match's status.

    Real NRL q-data has `match.stats.players.homeTeam/awayTeam` while some
    older formats have a sibling `players` map. Player identities live in
    `match.homeTeam.players/awayTeam.players`. Never source status from
    `match.stats.history.recentMatches`, which concerns unrelated games.
    """
    if not isinstance(payload,(dict,list)):return None,'Not JSON data'
    id_to_name=names_by_external_id(payload)
    candidates=[]
    saw_stats=0
    saw_status=0
    unresolved=0
    # A current-match object is only accepted when its OWN state is live/final.
    # Avoid treating statistics from unrelated recentMatches as a current match.
    for obj in current_match_containers(payload):
        if not isinstance(obj,dict):continue
        match=obj.get('match') if isinstance(obj.get('match'),dict) else obj
        state=match_state(match)
        participants=resolved_fixture(match,fixture)
        if not participants:
            if state:
                unresolved+=1
            continue
        by_name, by_id, eligible = roster_lookups(roster, participants)
        possible=[]
        # Official website's `match.stats.players` is the primary shape.
        match_stats=match.get('stats')
        if isinstance(match_stats,dict):
            possible.append(match_stats.get('players'))
        # Some older endpoints put the same player rows alongside `match`.
        possible.append(obj.get('players'))
        possible.extend(obj.get(k) for k in ('matchPlayerStats','playerStatistics'))
        if match is not obj:
            possible.append(match.get('players'))
        for stats in possible:
            found={}
            if isinstance(stats,dict) and isinstance(stats.get('homeTeam'),list) and isinstance(stats.get('awayTeam'),list):
                if not any(fantasy_of(row) is not None for side in ('homeTeam','awayTeam') for row in stats[side]):continue
                saw_stats+=1
                if not state:continue
                saw_status+=1
                found.update(scored_rows(stats['homeTeam'], participants['home'], by_name, by_id, id_to_name))
                found.update(scored_rows(stats['awayTeam'], participants['away'], by_name, by_id, id_to_name))
                candidates.append((state,found,participants,official_match_coverage(stats,participants,by_name,by_id,id_to_name)))
            elif isinstance(stats,list):
                if not any(fantasy_of(row) is not None for row in stats):continue
                saw_stats+=1
                if not state:continue
                saw_status+=1
                for side in (participants['home'],participants['away']):
                    found.update(scored_rows(stats,side,by_name,by_id,id_to_name))
                candidates.append((state,found,participants,None))
    if not candidates:
        if 'TBC' in (fixture['home'],fixture['away']) and unresolved:
            return None,'Knockout match nations not yet identified from official NRL current-match teams'
        return None,f'No current-match stats with live/final state (fantasy tables={saw_stats}, state-confirmed={saw_status})'
    # Prefer fully attested official split-side match tables for a final.
    # A partial official table remains useful for *live provisional* scoring.
    candidates.sort(key=lambda pair:(pair[0]=='final' and bool(pair[3] and pair[3]['complete']),len(pair[1])),reverse=True)
    state,scores,participants,coverage=candidates[0]
    if state=='final' and not probe:
        if not (coverage and coverage['complete']):
            return None,'Final match player table incomplete: need >=17 verified appearances, fantasy points and unambiguous identities per side'
        # Source must identify the CURRENT fixture's two nations explicitly.
        # Without this, a stale/historical game could be attributed to 2026.
        official=None
        for obj in current_match_containers(payload):
            if not isinstance(obj,dict):continue
            m=obj.get('match') if isinstance(obj.get('match'),dict) else obj
            if (isinstance(m.get('stats'),dict) and m['stats'].get('players') is not None) or (isinstance(obj.get('players'),dict) and m is not obj):
                if (nation_from_team(m.get('homeTeam'))==participants['home'] and
                    nation_from_team(m.get('awayTeam'))==participants['away']):
                    official=True
                    break
        if not official:
            return None,'Final match not attested to the 2026 fixture nations by official match state'
        coverage['official_teams']=True

    sides={p['nation'] for p in roster if p['id'] in scores}
    minimum=1 if probe else 8
    if len(scores)<minimum or (not probe and len(sides)<2):
        return None,f'Only {len(scores)} matched eligible players across {len(sides)} nations — insufficient verified match scope (fantasy tables={saw_stats})'
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    return {'home':participants['home'],'away':participants['away'],'status':state,'updatedAt':now,'players':scores, **({'coverage':coverage} if coverage else {})},f'{len(scores)} matched eligible player scores ({state})'

def parse_embedded(source,fixture,roster,*,probe=False):
    best=None; diagnostics=[]
    for payload in extract_embedded(source):
        result,msg=parse_payload(payload,fixture,roster,probe=probe)
        if result and (best is None or (result['status']=='final',len(result['players']))>(best['status']=='final',len(best['players']))):best=result
        diagnostics.append(msg)
    if best:return best, f'{len(best["players"])} match-scoped players'
    return None, (diagnostics[-1] if diagnostics else 'No match-scoped embedded JSON found')


def fetch_payloads_browser(url, *, timeout_ms=16000):
    """Diagnostic Playwright probe: include cross-domain JSON stats endpoints.

    Earlier versions discarded EVERY non-nrl.com response. Match statistics may
    be loaded from third-party hosts. Diagnostic log reveals host/path only
    (never credentials, querystrings, request headers, or payload values).
    """
    from playwright.sync_api import sync_playwright
    from collections import Counter
    samples=[]; hints=[]; observations=[]; domains=Counter(); failures=[]
    def safe_path(raw):
        parsed=urlparse(raw)
        return (parsed.hostname or 'unknown') + (parsed.path[:160] or '/')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36',
            viewport={'width':1440,'height':900},locale='en-AU')
        page=context.new_page()
        def capture(response):
            parsed=urlparse(response.url)
            host=parsed.hostname or 'unknown'
            domains[host]+=1
            if response.status>=400 and len(failures)<12:
                failures.append(f'{response.status} {safe_path(response.url)}')
            if response.status!=200 or len(samples)>=150:return
            content_type=response.headers.get('content-type','').lower()
            path=parsed.path.lower()
            # Some match APIs declare JSON as text/plain; still try likely paths.
            candidate=('json' in content_type or any(s in path for s in ('/stats','/match','/player','/data','/fixture')))
            if not candidate:return
            try:data=response.json()
            except Exception:return
            if not isinstance(data,(dict,list)):return
            samples.append((safe_path(response.url),data))
            serialized=json.dumps(data,ensure_ascii=False)[:1500000].lower()
            if any(k.lower() in serialized for k in FANTASY_KEYS):
                hints.append(safe_path(response.url))
        page.on('response',capture)
        try:
            page.goto(url,wait_until='domcontentloaded',timeout=timeout_ms)
            page.wait_for_timeout(6500)
            observations.append('Page title: '+str(page.title())[:120])
            observations.append('Rendered URL: '+safe_path(page.url))
            for js in ('window.__NEXT_DATA__','window.__INITIAL_STATE__','window.__PRELOADED_STATE__'):
                try:
                    data=page.evaluate(f'() => {js} || null')
                    if isinstance(data,(dict,list)):samples.append((js,data))
                except Exception:pass
            clicked=False
            for label in ('Player Stats','Player stats','Players Stats','Stats'):
                try:
                    # Prefer actual accessible tab/button controls, fall back to text.
                    loc=page.get_by_text(label,exact=True).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=2000)
                        clicked=True
                        page.wait_for_timeout(5000)
                        break
                except Exception:pass
            observations.append('Player Stats tab clickable: '+str(clicked))
            body=page.locator('body').inner_text(timeout=3000)
            observations.append('Rendered page contains Fantasy label: '+str(bool(re.search(r'fantasy\s*(points|pts)',body,re.I))))
            observations.append('Rendered page contains Player Stats label: '+str('player stats' in body.lower()))
            # Presence of the visible Fantasy selector alone does NOT prove
            # that real fantasy player scores have loaded.
            rendered=page.content()
            rsoup=BeautifulSoup(rendered,'html.parser')
            qnodes=rsoup.select('#vue-match-centre[q-data], [q-data]')
            observations.append('Rendered match-centre q-data elements: '+str(len(qnodes)))
            if qnodes:
                try:
                    stats=sum(1 for record in walk(json.loads(qnodes[0].get('q-data')))
                              if isinstance(record,dict) and fantasy_of(record) is not None)
                    observations.append('Rendered q-data fantasy stat records: '+str(stats))
                except (ValueError,TypeError) as exc:
                    observations.append('Rendered q-data parse failed: '+type(exc).__name__)
            samples.append(('rendered-html',rendered))
        finally:browser.close()
    observations.append('Response domains/counts: '+str(domains.most_common(18)))
    observations.append('HTTP errors: '+str(failures))
    observations.append('Candidate JSON endpoints (host/path only): '+str([orig for orig,_ in samples if orig!='rendered-html'][:22]))
    return samples,hints,observations


def parse_match_url(url,fixture,roster,*,browser=False,probe=False):
    u=urlparse(url)
    if u.scheme!='https' or u.hostname not in ('www.nrl.com','nrl.com') or not u.path.startswith('/draw/'):
        return None,'Invalid match URL',[]
    log=[]
    try:
        res=requests.get(url,timeout=12,headers=HEADERS)
        res.raise_for_status()
        soup=BeautifulSoup(res.text,'html.parser')
        qnodes=soup.select('#vue-match-centre[q-data], [q-data]')
        valid=[]
        for node in qnodes:
            try: valid.append(json.loads(node.get('q-data','')))
            except (ValueError,TypeError): pass
        stats=sum(1 for obj in valid for record in walk(obj)
                  if isinstance(record,dict) and fantasy_of(record) is not None)
        log.append(f'HTML match-centre q-data elements: {len(qnodes)}; decoded: {len(valid)}; fantasy stat records: {stats}')
        match,msg=parse_embedded(res.text,fixture,roster,probe=probe)
        log.append(f'HTML HTTP {res.status_code}: {msg}')
        if match:return match,msg,log
    except Exception as exc:log.append('HTML request failed: '+str(exc)[:130])
    if browser:
        try:
            samples,hints,observations=fetch_payloads_browser(url)
            for note in observations:log.append(note)
            log.append(f'Browser captured {len(samples)} payloads; fantasy-related JSON endpoints: {hints[:4]}')
            for origin,payload in samples:
                if isinstance(payload,str):match,msg=parse_embedded(payload,fixture,roster,probe=probe)
                else:match,msg=parse_payload(payload,fixture,roster,probe=probe)
                if match:
                    log.append('Matched from '+origin)
                    return match,msg,log
        except Exception as exc:log.append('Browser fallback failed: '+str(exc)[:160])
    return None,'No match-scoped fantasy points could be verified',log


def date_of(s):
    try:return datetime.fromisoformat(s).date()
    except (ValueError,TypeError):return None


def active_fixture(fixture, existing, today):
    # Date is the host-city calendar date; a one-day pre-match buffer covers
    # timezone boundaries, and seven days after the fixture allows stat fixes.
    d = date_of(fixture.get('date'))
    if not d:
        return False  # A missing date must never trigger a permanent scan.
    return today - timedelta(days=7) <= d <= today + timedelta(days=1)


def update(sources,roster,existing,*,fetcher=None, browser=False, today=None,source_proofs=None):
    now=datetime.now(timezone.utc);today=today or now.date()
    out=copy.deepcopy(existing);out.setdefault('rounds',{})
    count=0
    fixtures=sources['fixtures']
    assert len({f['id'] for f in fixtures})==len(fixtures), 'Duplicate fixture IDs'
    assert all(f.get('round') in (1,2,3,4,5) for f in fixtures), 'Invalid round'
    scheduled=[f for f in fixtures if active_fixture(f,existing,today)]
    print(f'SCORING SCHEDULE: {len(fixtures)} fixtures configured; {len(scheduled)} within match-day window')
    for fixture in scheduled:
        u=urlparse(fixture['url'])
        if u.scheme!='https' or u.hostname not in ('www.nrl.com','nrl.com') or not u.path.startswith('/draw/rugby-league-world-cup/2026/'):
            print('SKIP unapproved URL',fixture['id']);continue
        print('CHECK',fixture['id'],fixture['date'],fixture['home'],fixture['away'])
        if fetcher:
            try:
                s=fetcher(fixture['url'])
                match,msg=parse_embedded(s,fixture,roster)
            except Exception as exc:match,msg=None,str(exc)
            notes=[]
        else:
            # CRITICAL: Settlement requires fresh proof of each FULL-TIME match
            # at every scheduled run. The former abs(days)<=2 gate disabled
            # Playwright just as older match-centre pages became harder to
            # extract, leaving last_source_verified_at stale and R3 locked.
            # active_fixture() already bounds this to the 7-day match window.
            match,msg,notes=parse_match_url(fixture['url'],fixture,roster,browser=browser)
        for line in notes:print('  ',line)
        if not match:
            print('NO SOURCE DATA',fixture['id'],msg)
            continue
        # Transient attestation: this particular invocation verified the actual
        # NRL.com match centre. Unlike a cached JSON feed, this can establish
        # that an unchanged final score really was checked recently.
        if source_proofs is not None and match['status']=='final':
            source_proofs.append({'fixture_id':fixture['id'], 'status':'final',
                                  'players':match['players'],'coverage':match.get('coverage')})
        slot=out['rounds'].setdefault(str(fixture['round']),{'matches':{}})['matches']
        old=slot.get(fixture['id'])
        if old and old.get('status')=='final' and match['status']!='final':continue
        if (old and old.get('status')==match['status'] and old.get('players')==match['players']
                and old.get('coverage')==match.get('coverage')):
            last=datetime.fromisoformat(old.get('updatedAt','1970-01-01T00:00:00+00:00').replace('Z','+00:00'))
            if match['status']!='live' or now-last<timedelta(minutes=10):continue
        slot[fixture['id']]=match
        count+=1
        print('VERIFIED',fixture['id'],msg)
    # Never treat a green Actions run as evidence a round was settled.
    # This line reports how many match-scoped full-time sources were genuinely
    # reverified DURING THIS RUN; an unchanged cached JSON row is not proof.
    if source_proofs is not None:
        for rn in sorted({f['round'] for f in scheduled}):
            due=[f for f in scheduled if f['round']==rn]
            proofs_for_round={p['fixture_id'] for p in source_proofs if p['fixture_id'] in {f['id'] for f in due}}
            recorded=out.get('rounds',{}).get(str(rn),{}).get('matches',{})
            finals=sum(1 for f in due if recorded.get(f['id'],{}).get('status')=='final')
            print(f'RECHECK R{rn}: feed final={finals}/{len(due)}; fresh official final proofs={len(proofs_for_round)}/{len(due)}')
            if due and all(date_of(f.get('date')) < today for f in due) and len(proofs_for_round)<len(due):
                missing=sorted({f['id'] for f in due}-proofs_for_round)
                print('::warning title=World Cup fantasy source recheck incomplete::R'+str(rn)+
                      ' has no fresh final fantasy proof for '+', '.join(missing)+
                      '. The scoring database will keep this round provisional until all evidence is present.')
    if count:out['updatedAt']=now.isoformat(timespec='seconds')
    return out,count


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--probe-historical',action='store_true',help='Attempt real 2025 England–Kangaroos fixture without publishing')
    ap.add_argument('--browser',action='store_true',help='Use Chromium to capture NRL match-centre JSON after page loads')
    ap.add_argument('--dry-run',action='store_true')
    args=ap.parse_args()
    roster=json.loads((ROOT/'roster.json').read_text())
    if args.probe_historical:
        fixture={'id':'ENG-AUS-2025','round':0,'home':'ENG','away':'AUS'}
        url='https://www.nrl.com/draw/internationals/2025/game-1/england-v-kangaroos/'
        match,msg,notes=parse_match_url(url,fixture,roster,browser=True,probe=True)
        print('HISTORICAL PROBE:',msg)
        for line in notes:print(' ',line)
        if match:
            print('Historical match found:',match['status'],len(match['players']),'2026-roster players also in match')
            print('Sample IDs/points:',list(match['players'].items())[:8]);return 0
        print('Historical probe inconclusive. Do not interpret successful workflow as a validated score feed.')
        return 2
    source=json.loads((ROOT/'score_sources.json').read_text())
    path=ROOT/'worldcup-fantasy-scores.json'
    current=json.loads(path.read_text())
    proof_records=[]
    new,count=update(source,roster,current,browser=args.browser,source_proofs=proof_records)
    # DO NOT commit this file. The workflow passes it to the private Supabase
    # publisher during the same job, regardless of whether market scores changed.
    # If the NRL lookup fails, this is empty and settlement cannot proceed.
    (ROOT/'wcf_source_proofs_runtime.json').write_text(
        json.dumps({'proven':proof_records},ensure_ascii=False)+'\n')
    if count and not args.dry_run:
        path.write_text(json.dumps(new,ensure_ascii=False,indent=2)+'\n')
    print('Verified feed changes:',count)
    return 0

if __name__=='__main__':sys.exit(main())
