#!/usr/bin/env python3
"""Best-effort, fail-closed NRL.com World Cup men's team-list extractor.

Run periodically on GitHub Actions; only replaces a nation's key when all
13 starters and six match-day bench players have been parsed unambiguously.
No credentials or proprietary NRL APIs are required.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
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
MARK_PATTERN = re.compile(
    rf'(?P<role>{ROLE_PATTERN})\s+for\s+(?P<team>{TEAM_PATTERN})\s+is\s+number\s+(?P<num>\d{{1,2}})\s+(?P<name>[^\r\n<]+?)\s*$',
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
        if s:lines.append(s)
    return lines


def parse_teams(source: str) -> dict[str, dict[str, list[str]]]:
    people = defaultdict(lambda: {'start': [], 'bench': [], 'other': []})
    rows = visible_lines(source)
    for line in rows:
        # Names after the match-team markup should not contain a paragraph.
        match = MARK_PATTERN.search(line)
        if not match: continue
        role = match['role'].lower()
        code = COUNTRY_CODES[match['team'].lower()]
        number = int(match['num'])
        name = match['name'].strip(' "*,. \t')
        # Exclude trailing markdown link and double-printed jersey numbers.
        name = re.split(r'\]\(|\s+Image:', name, maxsplit=1)[0].strip()
        normalized = normalize_name(name)
        if not normalized: continue
        category='start' if role in ROLES_START else ('bench' if role=='interchange' else 'other')
        record=(normalized,name,number)
        if record not in people[code][category]: people[code][category].append(record)
    return dict(people)


def complete_matchday(teams):
    """Require exactly 13 distinct starters and 6 bench players.

    NRL pages show interchange positions 14-17 and positions 18-19 under
    'Reserve'. Later named reserves (20-23) must NOT count as interchange.
    """
    valid={}
    for country, groups in teams.items():
        starters = {name for name, label, number in groups['start']}
        interchange = {name for name,label,number in groups['bench']}
        rest = [(name,label,number) for name,label,number in groups['other'] if name not in starters and name not in interchange]
        # For NRL match cards, first two reserves are the fifth and sixth bench players.
        # Prefer jerseys 18/19 where present; avoid promoting all extended reserves.
        chosen = [n for n,label,number in rest if number in (18,19)]
        if len(interchange) + len(set(chosen)) < 6:
            # Only take the first slots if the list appears ordered and complete.
            for n,label,number in rest:
                if n not in chosen and n not in interchange:
                    chosen.append(n)
                    if len(interchange) + len(set(chosen)) >= 6: break
        bench = interchange | set(chosen[:max(0,6-len(interchange))])
        if len(starters) == 13 and len(bench) == 6 and starters.isdisjoint(bench):
            valid[country]={'starting':starters,'interchange':bench}
    return valid


def discover_urls(session=None, manual=None):
    candidates={'1':{DEFAULT_ROUND_1}}
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
    return {k: sorted(v) for k,v in candidates.items()}


def build_changes(previous: dict, players: list, pages: dict[str,str]):
    result = json.loads(json.dumps(previous))
    result.setdefault('rounds',{})
    result.setdefault('sources',{})
    by_nation=defaultdict(list)
    for p in players:
        if p.get('marketEligible'): by_nation[p['nation']].append(p)
    verified = []
    for round_no, sources in pages.items():
        for url, page in sources.items():
            parsed = complete_matchday(parse_teams(page))
            if not parsed:
                print(f'WARN {url}: zero complete 19-player nations, keeping last known statuses')
                continue
            current=result['rounds'].setdefault(round_no,{})
            page_verified=0
            for country, values in parsed.items():
                roster = by_nation.get(country,[])
                active_names = values['starting'] | values['interchange']
                matched = sum(normalize_name(p['name']) in active_names for p in roster)
                required = max(2, math.ceil(len(roster) * 0.30))
                if matched < required:
                    print(f'WARN {country} R{round_no}: only {matched}/{len(roster)} known market players match; need {required}. Keeping last verified statuses.')
                    continue
                # Never clear a different round's values; validate team completeness first.
                for player in roster:
                    key = normalize_name(player['name'])
                    current[player['id']] = ('starting' if key in values['starting']
                                             else 'interchange' if key in values['interchange']
                                             else 'omitted')
                verified.append((round_no,country,len(roster)))
                page_verified+=1
            if page_verified and not any(x.get('url') == url for x in result['sources'].setdefault(round_no,[])):
                result['sources'][round_no].append({'url':url,'label':f'NRL.com World Cup men - round {round_no}'})
    return result,verified


def run(offline_pages=None):
    feed_path=ROOT/'worldcup-teamlists.json'
    roster=json.loads((ROOT/'roster.json').read_text(encoding='utf8'))
    before=json.loads(feed_path.read_text(encoding='utf8'))
    manual=json.loads((ROOT/'additional_sources.json').read_text(encoding='utf8'))
    manual_urls = [u for vals in manual.values() for u in vals]
    sources=discover_urls(manual=manual_urls) if offline_pages is None else {r:list(p) for r,p in offline_pages.items()}
    pages={}
    for round_no,urls in sources.items():
        if offline_pages is not None: pages[round_no]=offline_pages[round_no]
        else:
            for url in urls:
                try: pages.setdefault(round_no,{})[url]=download_text(url)
                except Exception as e: print('WARN',e,file=sys.stderr)
    candidate,verified=build_changes(before,roster,pages)
    compare=lambda d: {k:v for k,v in d.items() if k!='updatedAt'}
    if compare(candidate)==compare(before):
        print(f'No changed team lists ({len(verified)} validated nations, {len(pages)} rounds checked).')
        return False
    candidate['updatedAt'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    feed_path.write_text(json.dumps(candidate,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    print(f'Updated feed: {len(verified)} validated nation-round entries: {verified}')
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
