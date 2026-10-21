#!/usr/bin/env python3
"""Best-effort, fail-closed NRL.com World Cup men's team-list extractor.

Run periodically on GitHub Actions; only replaces a nation's key when all
13 starters and six match-day bench players have been parsed unambiguously.
No credentials or proprietary NRL APIs are required.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import html
import math
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
NEWS_URLS = [
    'https://www.nrl.com/news/',
    'https://www.nrl.com/news/topic/team-lists/',
]
DEFAULT_ROUND_1 = 'https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-1/'
# Article slugs and projected publication Tuesdays. Future URLs are *candidates*,
# never treated as published until the article can be downloaded and verified.
# Index discovery still takes precedence if the official date or title differs.
ARTICLE_SCHEDULE = {
    '2': (date(2026, 10, 20), ('world-cup-team-lists-mens-round-2',)),
    '3': (date(2026, 10, 27), ('world-cup-team-lists-mens-round-3',)),
    '4': (date(2026, 11, 3), ('world-cup-team-lists-mens-semi-finals',
                             'world-cup-team-lists-mens-semi-final',
                             'world-cup-team-lists-mens-finals-week-1')),
    '5': (date(2026, 11, 10), ('world-cup-team-lists-mens-final',
                              'world-cup-team-lists-mens-grand-final')),
}

COUNTRY_CODES = {
    'australia':'AUS', 'kangaroos':'AUS', 'new zealand':'NZL', 'kiwis':'NZL',
    'england':'ENG', 'tonga':'TON', 'samoa':'SAM', 'papua new guinea':'PNG',
    'png':'PNG', 'kumuls':'PNG', 'fiji':'FIJ', 'france':'FRA', 'lebanon':'LEB',
    'cook islands':'COK',
}
ROLES_START = ['fullback','winger','centre','five-eighth','five eighth','halfback','prop','hooker','2nd row','second row','lock']
ROLES_BENCH = ['interchange','replacement','reserve']
ROLE_PATTERN = '|'.join(re.escape(s) for s in (ROLES_START + ROLES_BENCH))
TEAM_PATTERN = '|'.join(re.escape(s) for s in sorted(COUNTRY_CODES, key=len, reverse=True))
# Team-list mirrors sometimes flatten the two columns into ONE line:
# "Fullback for Tonga is number 1 Lehi Hopoate 1 Fullback for Lebanon is
#  number 1 Jacob Kiraz".  Match each entry's *start* and split at the next
# start; an end-of-line anchored name regex swallows the opposing player.
ENTRY_START_PATTERN = re.compile(
    rf'(?P<role>{ROLE_PATTERN})\s+for\s+(?P<team>{TEAM_PATTERN})'
    rf'\s+is\s+number\s+(?P<num>\d{{1,2}})\s+',
    re.I,
)
URL_PATTERN = re.compile(r'https?://(?:www\.)?nrl\.com/news/2026/\d\d/\d\d/[^\s"<>\)]+', re.I)
SLUG_PATTERN = re.compile(r'/world-cup-team-lists-(?:mens|men-s|men|mens-).*?/?$', re.I)
TIMEOUT = 25


def normalize_name(name: str) -> str:
    n = ''.join(c for c in unicodedata.normalize('NFKD', html.unescape(str(name))) if not unicodedata.combining(c))
    n = n.replace('’', "'").replace('‘', "'").replace('ʻ', "'").replace('`', "'")
    n = re.sub(r'[^a-zA-Z0-9]', '', n).lower()
    aliases = {'sualauvifaalogo':'suafaalogo', 'kennethlio':'kliro'}
    return aliases.get(n, n)


def round_for_url(url: str) -> str | None:
    slug = urlparse(url).path.lower().strip('/')
    if not re.search(r'world-cup-team-lists-(?:mens|men-s)(?:-|/|$)', slug): return None
    for i in (1,2,3):
        if re.search(rf'round-{i}(?:$|[-/])', slug): return str(i)
    if 'semi' in slug or 'finals-week-1' in slug: return '4'
    if 'final' in slug and 'semi' not in slug: return '5'
    return None


def valid_nrl_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == 'https' and p.hostname in ('www.nrl.com','nrl.com') and p.path.startswith('/news/2026/')


def download_text(url: str, session=None) -> str:
    sess = session or requests.Session()
    paths = [url, 'https://r.jina.ai/' + url]  # readable fallback for NRL pages that block bots
    last_error = None
    for candidate in paths:
        try:
            response = sess.get(candidate, timeout=TIMEOUT, headers={'User-Agent': 'Mozilla/5.0 (compatible; WorldCupFantasyTeamListChecker/1.0; stats-only)'})
            response.raise_for_status()
            if len(response.content) < 300: raise ValueError('Page too short')
            return response.text
        except Exception as e:
            last_error = e
    raise RuntimeError(f'Could not retrieve {url}: {last_error}')



def get_official_source(url: str, session=None, expected_nations=10, render=None) -> str:
    """Select the *extractable* article, not the first HTTP 200 page.

    NRL may reply 200 with a JS shell or anti-bot page. Such a response has
    hundreds of bytes but no verifiable starting 13 + interchange 6.
    Try the normal page, a text mirror, and finally a rendered Chromium page.
    Always return only the version with the most validated nations; return an
    empty string if none can be verified. Fail closed in build_changes().
    """
    sess = session or requests.Session()
    targets=[('direct',url),('readable','https://r.jina.ai/'+url)]
    best_source=''
    best_count=0
    best_method='none'
    for method, address in targets:
        try:
            r=sess.get(address, timeout=TIMEOUT, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                              '(KHTML, like Gecko) Chrome/131.0 Safari/537.36',
                'Accept': 'text/html,text/plain;q=0.9,*/*;q=0.7',
            })
            r.raise_for_status()
            source=r.text if len(r.content)>300 else ''
            verified=complete_matchday(parse_teams(source)) if source else {}
            print(f'Source check {method} R{round_for_url(url) or "?"}: '
                  f'{len(verified)} complete nations, HTTP {r.status_code}, '
                  f'{len(r.content)} bytes')
            if len(verified)>best_count:
                best_source,best_count,best_method=source,len(verified),method
            if best_count>=expected_nations:break
        except Exception as e:
            print(f'WARN {method} fetch {url}: {e}', file=sys.stderr)
    if best_count<expected_nations:
        try:
            rendered=render(url) if render is not None else render_nrl_browser(url)
            verified=complete_matchday(parse_teams(rendered))
            print(f'Source check browser R{round_for_url(url) or "?"}: '
                  f'{len(verified)} complete nations')
            if len(verified)>best_count:
                best_source,best_count,best_method=rendered,len(verified),'browser'
        except Exception as e:
            print(f'WARN browser fetch {url}: {e}',file=sys.stderr)
    if not best_count:
        print(f'::warning::No verified 19-player teams in {url}. '
              'No changes published from this source.',file=sys.stderr)
    else:
        print(f'Validated {best_count} complete nations from {best_method} for {url}')
    return best_source


def render_nrl_browser(url: str) -> str:
    """Fallback used only if ordinary and readable pages are incomplete."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page=browser.new_page(locale='en-AU',
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/131.0 Safari/537.36')
            page.goto(url,wait_until='domcontentloaded',timeout=30000)
            try:
                page.wait_for_function("""() => {
                  const marked = document.querySelector('[alt*=" for "][alt*=" is number "], [aria-label*=" for "][aria-label*=" is number "]');
                  return !!marked || /(?:Fullback|Winger|Centre|Prop|Hooker) for (?:Tonga|Australia|France|England|Samoa|Lebanon|Fiji|New Zealand|Papua New Guinea|Cook Islands) is number/i.test(document.body?.innerText || '');
                }""",timeout=13000)
            except Exception:
                pass
            return page.content()
        finally:
            browser.close()


def visible_lines(source: str) -> list[str]:
    soup = BeautifulSoup(source, 'html.parser')
    # NRL team cards expose meaningful descriptions in alt/aria-label attributes.
    for script in soup(['script','style']): script.decompose()
    labels = [node.get('alt') or node.get('aria-label') for node in soup.find_all(attrs={'alt':True})]
    labels += [node.get('aria-label') for node in soup.find_all(attrs={'aria-label':True})]
    labels += soup.get_text('\n', strip=True).splitlines()
    # Jina.ai fallback is already markdown; preserve its original lines too.
    if '<html' not in source.lower() and '<!doctype' not in source.lower():
        labels += source.splitlines()
    lines=[]
    for line in labels:
        if not line:continue
        s=html.unescape(str(line)).strip().lstrip('* -').strip()
        # Readable NRL mirrors commonly express jersey descriptions as
        # Markdown image alt text or inline links, not plain lines.
        for text in re.findall(r'!?\[([^\]]{8,180})\]\([^)]*\)', s):
            t=text.strip()
            if t:lines.append(t)
        s=re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', s)
        if s:lines.append(s)
    return lines


def parse_teams(source: str) -> dict[str, dict[str, list[str]]]:
    people = defaultdict(lambda: {'start': [], 'bench': [], 'other': []})
    rows = visible_lines(source)
    for line in rows:
        matches = list(ENTRY_START_PATTERN.finditer(line))
        for i, match in enumerate(matches):
            role = match['role'].lower()
            code = COUNTRY_CODES[match['team'].lower()]
            number = int(match['num'])
            # Each player name stops at the NEXT role/team/jersey entry, not
            # necessarily at a newline (Jina often joins opposing teams).
            end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            name = line[match.end():end].strip(' "*,. \t')
            if i + 1 < len(matches):
                # The collapsed table includes the jersey number in its
                # separator, e.g. "Lehi Hopoate 1 Fullback for Lebanon...".
                # Some games print two numbers during late jersey changes.
                name = re.sub(r'(?:\s+\d{1,2}){1,3}\s*$', '', name)
            name = re.split(r'\]\(|\s+Image:', name, maxsplit=1)[0].strip(' "*,. \t')
            # Refuse empty/number-only entries; never guess a player name.
            if not re.search(r'[a-zA-Z]', name):
                continue
            normalized = normalize_name(name)
            category = ('start' if role in ROLES_START
                        else 'bench' if role == 'interchange' else 'other')
            record = (normalized, name, number)
            if record not in people[code][category]:
                people[code][category].append(record)
    return dict(people)


def complete_matchday(teams):
    """Require 13 distinct starters and 6 actual match-day bench players.

    Jersey numbers may shift due to late changes, so prefer the official
    Interchange role. Only jersey 18-19 Reserve rows may supplement it.
    Never turn Reserve jersey 20+ into an interchange player.
    """
    valid={}
    for country, groups in teams.items():
        starters = {name for name, label, number in groups['start']}
        interchange = {name for name,label,number in groups['bench']}
        rest = [(name,label,number) for name,label,number in groups['other'] if name not in starters and name not in interchange]
        # Only jerseys 18-19 labelled Reserve may fill a missing matchday
        # bench slot. NEVER promote reserve #20+; they are outside the 19.
        chosen = {n for n,label,number in rest if number in (18,19)}
        bench = interchange | chosen
        if len(starters) == 13 and len(bench) == 6 and starters.isdisjoint(bench):
            valid[country]={'starting':starters,'interchange':bench}
    return valid


def prospective_urls(round_no: str, today: date) -> list[str]:
    """Bounded fallback if official NRL news indexes omit the new article.

    Probe the projected publication Tuesday and next day, only once these
    dates have actually arrived. Does not assume that any article exists.
    """
    planned, slugs = ARTICLE_SCHEDULE[round_no]
    if today < planned:
        return []
    dates = [d for d in (planned, planned + timedelta(days=1)) if d <= today]
    return [f'https://www.nrl.com/news/{d:%Y/%m/%d}/{slug}/'
            for d in dates for slug in slugs]


def discover_urls(session=None, manual=None, previous=None, today=None):
    candidates={'1':{DEFAULT_ROUND_1}}
    today = today or datetime.now(timezone.utc).date()
    previous = previous or {}
    # Revisit verified source articles to detect late team-list revisions.
    for rnd, sources in previous.get('sources', {}).items():
        for item in sources:
            url = item.get('url', '')
            if valid_nrl_url(url) and round_for_url(url) == rnd:
                candidates.setdefault(rnd, set()).add(url)
    for u in manual or []:
        if valid_nrl_url(u):
            r=round_for_url(u)
            if r: candidates.setdefault(r,set()).add(u)
    for url in NEWS_URLS:
        try:
            page=download_text(url,session)
        except Exception as e:
            print(f'WARN news discovery failed: {e}',file=sys.stderr)
            continue
        soup=BeautifulSoup(page,'html.parser')
        urls = [urljoin('https://www.nrl.com',a.get('href')) for a in soup.select('a[href]')]
        urls.extend(URL_PATTERN.findall(page))
        for candidate in urls:
            candidate = candidate.split('?',1)[0].rstrip('.,')
            if not valid_nrl_url(candidate): continue
            r = round_for_url(candidate)
            if r: candidates.setdefault(r,set()).add(candidate)
    # Deterministic fallback prevents a green Action with no new round merely
    # because NRL's homepage/team-list topic did not link to its published URL.
    for rnd in ARTICLE_SCHEDULE:
        # No need to guess article dates when a verified/manual URL is known.
        if rnd not in candidates:
            candidates[rnd] = set(prospective_urls(rnd, today))
    return {k: sorted(v) for k,v in candidates.items() if v}


def build_changes(previous: dict, players: list, pages: dict[str,str]):
    result = json.loads(json.dumps(previous))
    result.setdefault('rounds',{})
    result.setdefault('sources',{})
    by_nation=defaultdict(list)
    for p in players:
        if p.get('marketEligible'): by_nation[p['nation']].append(p)
    verified = []
    # After a later round has published, older round snapshots become
    # historical and must not be silently rewritten by NRL page revisions.
    existing_rounds = [int(r) for r, values in previous.get('rounds', {}).items()
                       if values and str(r).isdigit()]
    newest_published = max(existing_rounds, default=0)
    for round_no in sorted(pages, key=int, reverse=True):
        sources=pages[round_no]
        if int(round_no) < newest_published:
            continue
        for url, page in sources.items():
            parsed = complete_matchday(parse_teams(page))
            if not parsed:
                print(f'WARN {url}: zero complete 19-player nations, keeping last known statuses')
                continue
            page_verified=0
            for country, values in parsed.items():
                roster = by_nation.get(country,[])
                active_names = values['starting'] | values['interchange']
                matched = sum(normalize_name(p['name']) in active_names for p in roster)
                required = max(2, math.ceil(len(roster) * 0.30))
                if matched < required:
                    print(f'WARN {country} R{round_no}: only {matched}/{len(roster)} known market players match; need {required}. Keeping last verified statuses. Sample extracted names: {sorted(active_names)[:4]}')
                    continue
                # Never create/alter a round until a nation is actually verified.
                current=result['rounds'].setdefault(round_no,{})
                for player in roster:
                    key = normalize_name(player['name'])
                    current[player['id']] = ('starting' if key in values['starting']
                                             else 'interchange' if key in values['interchange']
                                             else 'omitted')
                verified.append((round_no,country,len(roster)))
                page_verified+=1
                newest_published=max(newest_published,int(round_no))
            if page_verified and not any(x.get('url') == url for x in result['sources'].setdefault(round_no,[])):
                result['sources'][round_no].append({'url':url,'label':f'NRL.com World Cup men - round {round_no}'})
    return result,verified


def run(offline_pages=None):
    feed_path=ROOT/'worldcup-teamlists.json'
    roster=json.loads((ROOT/'roster.json').read_text(encoding='utf8'))
    before=json.loads(feed_path.read_text(encoding='utf8'))
    manual=json.loads((ROOT/'additional_sources.json').read_text(encoding='utf8'))
    manual_urls = [u for vals in manual.values() for u in vals]
    sources=discover_urls(manual=manual_urls, previous=before) if offline_pages is None else {r:list(p) for r,p in offline_pages.items()}
    pages={}
    for round_no,urls in sources.items():
        if offline_pages is not None: pages[round_no]=offline_pages[round_no]
        else:
            # Complete fixture cards: 10 nations in R1/R2/R3, four in SF, two in Final.
            expected={'1':10,'2':10,'3':10,'4':4,'5':2}.get(round_no,1)
            for url in urls:
                source=get_official_source(url,expected_nations=expected)
                if source: pages.setdefault(round_no,{})[url]=source
    candidate,verified=build_changes(before,roster,pages)
    # Report complete per-nation coverage, not only whether the round key exists.
    market_nations=defaultdict(set)
    for p in roster:
        if p.get('marketEligible'):
            market_nations[p['nation']].add(p['id'])
    for rnd in sorted(set(sources) | set(candidate.get('rounds',{}))):
        keys=set(candidate.get('rounds',{}).get(rnd,{}))
        if not keys: continue
        full={nation for nation,ids in market_nations.items() if ids.issubset(keys)}
        print(f'Coverage R{rnd}: {len(full)}/{len(market_nations)} nations; '
              f'{len(keys)} player statuses')
        if len(full)<len(market_nations):
            missing_nations=sorted(set(market_nations)-full)
            print(f'::warning::R{rnd} incomplete nation coverage: '
                  f'{", ".join(missing_nations)}',file=sys.stderr)
    today = datetime.now(timezone.utc).date()
    missing=[]
    for rnd,(expected_date, _slugs) in ARTICLE_SCHEDULE.items():
        if today >= expected_date + timedelta(days=1) and not candidate.get('rounds',{}).get(rnd):
            missing.append(rnd)
            print(f'::error::Round {rnd} has no validated player statuses. '
                  'The official source may be blocked or its format may have changed.',file=sys.stderr)
    compare=lambda d: {k:v for k,v in d.items() if k!='updatedAt'}
    if compare(candidate)==compare(before):
        print(f'No changed team lists ({len(verified)} validated nations, {len(pages)} rounds checked).')
        if missing: raise RuntimeError('Missing expected round(s): '+', '.join(missing))
        return False
    candidate['updatedAt'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    feed_path.write_text(json.dumps(candidate,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    print(f'Updated feed: {len(verified)} validated nation-round entries: {verified}')
    if missing: raise RuntimeError('Missing expected round(s): '+', '.join(missing))
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--fixture-dir',type=Path,help='Use locally saved pages for offline verification')
    args=parser.parse_args()
    if args.fixture_dir:
        sources={}
        for p in sorted(args.fixture_dir.glob('round[1-5]_*.txt')):
            rd=p.name[5]
            sources.setdefault(rd,{})[f'https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-{rd}/']=p.read_text(encoding='utf8')
        run(sources)
    else:
        run()
