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


def scored_rows(rows, side, lookup_name, lookup_id, id_to_name):
    out={}
    if not isinstance(rows,list):return out
    for row in rows:
        if not isinstance(row,dict):continue
        pts=fantasy_of(row)
        if pts is None:continue
        pid=id_of(row)
        nm=identify(row) or (id_to_name.get(pid) if pid else None)
        person=lookup_name.get((side,normalise(nm))) if nm else None
        if not person and pid:
            candidate=lookup_id.get(pid)
            if candidate and candidate['nation']==side:person=candidate
        if not person:continue
        mins=row.get('minutesPlayed',row.get('mins'))
        if mins is None and isinstance(row.get('playerStats'),dict):mins=row['playerStats'].get('minutesPlayed')
        played=True
        if mins is not None:
            try:played=float(mins)>0
            except (ValueError,TypeError):pass
        out[person['id']]={'points':pts,'played':played}
    return out


def parse_payload(payload, fixture, roster, *, probe=False):
    """Parse match-scoped player stats. Handles NRL's ID-only fantasy rows.

    A separate lineup maps NRL playerId to name. Don't scan generic season
    tables, or infer game state from unrelated 'recentMatches' elements.
    """
    if not isinstance(payload,(dict,list)):return None, 'Not JSON data'
    by_name, by_id, eligible = roster_lookups(roster,fixture)
    id_to_name=names_by_external_id(payload)
    candidates=[]
    for obj in walk(payload):
        if not isinstance(obj,dict):continue
        # NRL match-centre pattern: players.homeTeam[] and awayTeam[] hold
        # fantasyPointsTotal, alongside match.matchState and lineup metadata.
        stats=obj.get('players')
        groups=None
        if isinstance(stats,dict) and isinstance(stats.get('homeTeam'),list) and isinstance(stats.get('awayTeam'),list):
            groups=stats
        if groups is not None:
            state=match_state(obj.get('match',{})) or match_state(obj)
            if not state:continue
            found={}
            found.update(scored_rows(groups['homeTeam'], fixture['home'], by_name, by_id, id_to_name))
            found.update(scored_rows(groups['awayTeam'], fixture['away'], by_name, by_id, id_to_name))
            candidates.append((state,found))
        # More generic form: a match object with player stats array and a
        # state stored in the SAME match-scope record.
        elif isinstance(stats,list):
            state=match_state(obj.get('match',{})) or match_state(obj)
            if not state:continue
            found={}
            for side in (fixture['home'],fixture['away']):
                found.update(scored_rows(stats,side,by_name,by_id,id_to_name))
            candidates.append((state,found))
        # Some APIs call it teamStats, matchPlayerStats, or playerStatistics.
        else:
            for key in ('matchPlayerStats','playerStatistics'):
                rows=obj.get(key)
                if not isinstance(rows,list):continue
                state=match_state(obj.get('match',{})) or match_state(obj)
                if not state:continue
                found={}
                for side in (fixture['home'],fixture['away']):
                    found.update(scored_rows(rows,side,by_name,by_id,id_to_name))
                candidates.append((state,found))
    if not candidates:return None,'No match-scoped fantasy player data and live/final state found'
    state,scores=max(candidates,key=lambda pair:len(pair[1]))
    sides=set()
    for p in roster:
        if p['id'] in scores:sides.add(p['nation'])
    minimum=1 if probe else 8
    if len(scores)<minimum or (not probe and len(sides)<2):
        return None,f'Only {len(scores)} eligible players across {len(sides)} nations — insufficient verified match scope'
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    return {'home':fixture['home'],'away':fixture['away'],'status':state,'updatedAt':now,'players':scores},f'{len(scores)} verified 2026-market player scores ({state})'


def parse_embedded(source,fixture,roster):
    best=None; diagnostics=[]
    for payload in extract_embedded(source):
        result,msg=parse_payload(payload,fixture,roster)
        if result and (best is None or len(result['players'])>len(best['players'])):best=result
        diagnostics.append(msg)
    if best:return best, f'{len(best["players"])} match-scoped players'
    return None, (diagnostics[-1] if diagnostics else 'No match-scoped embedded JSON found')


def fetch_payloads_browser(url, *, timeout_ms=16000):
    """Fallback for client-rendered NRL match pages. Captures JSON responses,
    including undocumented stats endpoints, without guessing endpoint paths.
    """
    from playwright.sync_api import sync_playwright
    samples=[]; hints=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=browser.new_context(user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36')
        page=context.new_page()
        def capture(response):
            if len(samples)>=50:return
            try:
                u=urlparse(response.url)
                if u.hostname not in ('nrl.com','www.nrl.com') and not u.hostname.endswith('.nrl.com'):return
                if response.status!=200:return
                typ=response.headers.get('content-type','').lower()
                if 'json' not in typ:return
                data=response.json()
                if isinstance(data,(dict,list)):
                    samples.append((response.url,data))
                    if 'fantasyPointsTotal' in str(data)[:500000]:hints.append(response.url)
            except Exception:pass
        page.on('response',capture)
        try:
            page.goto(url,wait_until='domcontentloaded',timeout=timeout_ms)
            page.wait_for_timeout(4000)
            for js in ('window.__NEXT_DATA__','window.__INITIAL_STATE__','window.__PRELOADED_STATE__'):
                try:
                    data=page.evaluate(f'() => {js} || null')
                    if isinstance(data,(dict,list)):samples.append((js,data))
                except Exception:pass
            # Fixture pages sometimes load the Player Stats tab only when opened.
            for label in ('Player Stats','Player stats'):
                try:
                    loc=page.get_by_text(label,exact=True).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=1500);page.wait_for_timeout(2500);break
                except Exception:pass
            samples.append(('rendered-html',page.content()))
        finally:browser.close()
    return samples,hints


def parse_match_url(url,fixture,roster,*,browser=False):
    u=urlparse(url)
    if u.scheme!='https' or u.hostname not in ('www.nrl.com','nrl.com') or not u.path.startswith('/draw/'):
        return None,'Invalid match URL',[]
    log=[]
    try:
        res=requests.get(url,timeout=12,headers=HEADERS)
        res.raise_for_status()
        match,msg=parse_embedded(res.text,fixture,roster)
        log.append(f'HTML HTTP {res.status_code}: {msg}')
        if match:return match,msg,log
    except Exception as exc:log.append('HTML request failed: '+str(exc)[:130])
    if browser:
        try:
            samples,hints=fetch_payloads_browser(url)
            log.append(f'Browser captured {len(samples)} payloads; fantasy-related JSON endpoints: {hints[:4]}')
            for origin,payload in samples:
                if isinstance(payload,str):match,msg=parse_embedded(payload,fixture,roster)
                else:match,msg=parse_payload(payload,fixture,roster)
                if match:
                    log.append('Matched from '+origin)
                    return match,msg,log
        except Exception as exc:log.append('Browser fallback failed: '+str(exc)[:160])
    return None,'No match-scoped fantasy points could be verified',log


def date_of(s):
    try:return datetime.fromisoformat(s).date()
    except (ValueError,TypeError):return None


def active_fixture(fixture,existing,today):
    old=existing.get('rounds',{}).get(str(fixture['round']),{}).get('matches',{}).get(fixture['id'],{})
    if old.get('status')=='final' and date_of(fixture.get('date','')) and date_of(fixture['date'])<today-timedelta(days=4):return False
    d=date_of(fixture.get('date',''))
    if not d:return True
    return today-timedelta(days=3)<=d<=today+timedelta(days=1)


def update(sources,roster,existing,*,fetcher=None, browser=False, today=None):
    now=datetime.now(timezone.utc);today=today or now.date()
    out=copy.deepcopy(existing);out.setdefault('rounds',{})
    count=0
    for fixture in sources['fixtures']:
        if not active_fixture(fixture,existing,today):continue
        u=urlparse(fixture['url'])
        if u.scheme!='https' or u.hostname not in ('www.nrl.com','nrl.com') or not u.path.startswith('/draw/rugby-league-world-cup/2026/'):
            print('SKIP unapproved URL',fixture['id']);continue
        if fetcher:
            try:
                s=fetcher(fixture['url'])
                match,msg=parse_embedded(s,fixture,roster)
            except Exception as exc:match,msg=None,str(exc)
            notes=[]
        else:match,msg,notes=parse_match_url(fixture['url'],fixture,roster,browser=browser)
        for line in notes:print('  ',line)
        if not match:
            print('NO SOURCE DATA',fixture['id'],msg)
            continue
        slot=out['rounds'].setdefault(str(fixture['round']),{'matches':{}})['matches']
        old=slot.get(fixture['id'])
        if old and old.get('status')=='final' and match['status']!='final':continue
        if old and old.get('status')==match['status'] and old.get('players')==match['players']:
            last=datetime.fromisoformat(old.get('updatedAt','1970-01-01T00:00:00+00:00').replace('Z','+00:00'))
            if match['status']!='live' or now-last<timedelta(minutes=10):continue
        slot[fixture['id']]=match
        count+=1
        print('VERIFIED',fixture['id'],msg)
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
        match,msg,notes=parse_match_url(url,fixture,roster,browser=True)
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
    new,count=update(source,roster,current,browser=args.browser)
    if count and not args.dry_run:
        path.write_text(json.dumps(new,ensure_ascii=False,indent=2)+'\n')
    print('Verified feed changes:',count)
    return 0

if __name__=='__main__':sys.exit(main())
