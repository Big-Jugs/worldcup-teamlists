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
from zoneinfo import ZoneInfo
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
# Confirmed official men's knockout article; poll THIS page again for late changes.
KNOWN_OFFICIAL_URLS = {
    '4': 'https://www.nrl.com/news/2026/11/03/world-cup-team-lists-mens-semi-finals/',
}
# Round 4 is only complete when all four named semi-final nations are verified.
ROUND_4_NATIONS = frozenset({'NZL', 'TON', 'AUS', 'SAM'})


def current_target_round(today: date) -> str:
    """Restrict polling to the relevant round; don't re-fetch historic pages."""
    for rnd, (start, _slugs) in sorted(ARTICLE_SCHEDULE.items(), key=lambda item: item[1][0], reverse=True):
        if today >= start:
            return rnd
    if today >= date(2026, 10, 13):
        return '1'
    return '1'



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
    """Prefer current official NRL HTML/Chromium over possibly cached text mirrors.

    Only accept a complete official 13+6 sheet. A third-party readable mirror
    is a last resort; its freshness cannot be independently guaranteed.
    """
    sess = session or requests.Session()
    # Match the lightweight request that returned the full NRL article in
    # the GitHub-runner diagnostic (147 KB rather than the 883-byte shell).
    headers = {'User-Agent': 'Mozilla/5.0'}

    def fetch_checked(method, address):
        try:
            r = sess.get(address, timeout=TIMEOUT, headers=headers)
            r.raise_for_status()
            source = r.text if len(r.content) > 300 else ''
            parsed = parse_teams(source) if source else {}
            complete = complete_matchday(parsed)
            card_count = len(BeautifulSoup(source, 'html.parser').select('.team-list-profile__name')) if source else 0
            partial_counts = {code: (len(groups['start']), len(groups['bench']), len(groups['other']))
                              for code, groups in sorted(parsed.items())}
            print(f'Source check {method} R{round_for_url(url) or "?"}: '
                  f'{len(complete)} complete nations, HTTP {r.status_code}, '
                  f'{len(r.content)} bytes; HTML team cards={card_count}; '
                  f'parsed start/bench/reserve={partial_counts}')
            return source, len(complete)
        except Exception as e:
            print(f'WARN {method} fetch {url}: {e}', file=sys.stderr)
            return '', 0

    direct, count = fetch_checked('official HTTP', url)
    if count >= expected_nations:
        return direct

    try:
        rendered = render(url) if render is not None else render_nrl_browser(url)
        complete = complete_matchday(parse_teams(rendered))
        print(f'Source check official browser R{round_for_url(url) or "?"}: '
              f'{len(complete)} complete nations')
        if len(complete) >= expected_nations:
            return rendered
    except Exception as e:
        print(f'WARN official browser fetch {url}: {e}', file=sys.stderr)

    readable, count = fetch_checked('readable mirror (freshness not guaranteed)',
                                    'https://r.jina.ai/' + url)
    if count >= expected_nations:
        print('::warning::Using readable NRL.com mirror: source may lag late official changes. '
              'The next refresh will re-check the live official page first.')
        return readable

    print(f'::warning::Round {round_for_url(url) or "?"}: no complete official '
          f'team list from {url}; preserving last verified feed.', file=sys.stderr)
    return ''


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
                  const marked = document.querySelector('.team-list-profile__name, [alt*=" for "][alt*=" is number "], [aria-label*=" for "][aria-label*=" is number "]');
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
    # Official NRL article cards split the accessibility line (e.g.
    # 'Fullback for New Zealand is number 1') and the player's first/surname
    # across nested spans. Reading separate text lines loses the relationship.
    # Read the ENTIRE card first. This covers both server-rendered pages and
    # the browser-rendered version, without relaxing 13+6 verification.
    labels = []
    for node in soup.select('.team-list-profile__name'):
        card_text = ' '.join(node.get_text(' ', strip=True).split())
        if ENTRY_START_PATTERN.search(card_text):
            labels.append(card_text)
    labels += [node.get('alt') or node.get('aria-label') for node in soup.find_all(attrs={'alt':True})]
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


def discover_urls(session=None, manual=None, previous=None, today=None, focus_round=None):
    candidates={'1':{DEFAULT_ROUND_1}}
    today = today or datetime.now(ZoneInfo('Australia/Sydney')).date()
    previous = previous or {}
    # Always probe the known, official semi-final article, even when the NRL
    # news homepage does not link to it. Treat it as a candidate, not proof.
    if focus_round in KNOWN_OFFICIAL_URLS:
        candidates.setdefault(focus_round, set()).add(KNOWN_OFFICIAL_URLS[focus_round])
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
    # A known/current official article is more reliable and quicker than
    # re-discovering a news index every five minutes. Unknown R5 URLs still
    # get discovery until an official article is located.
    should_discover = not focus_round or not candidates.get(focus_round)
    for url in NEWS_URLS if should_discover else []:
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
        if focus_round and rnd != focus_round:
            continue
        # No need to guess article dates when a verified/manual URL is known.
        if rnd not in candidates:
            candidates[rnd] = set(prospective_urls(rnd, today))
    return {k: sorted(v) for k,v in candidates.items() if v and (not focus_round or k == focus_round)}


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
        if int(round_no) < newest_published:
            continue
        sources = pages[round_no]
        for url, page in sources.items():
            parsed = complete_matchday(parse_teams(page))
            if not parsed:
                print(f'WARN {url}: no complete 19-player nation; last verified feed unchanged')
                continue

            # For knockout rounds, a single source must contain the complete
            # official matchday sheet; do not mix old and new partial pages.
            if round_no == '4' and set(parsed) != ROUND_4_NATIONS:
                print(f'::warning::R4 source incomplete; got {sorted(parsed)}, need {sorted(ROUND_4_NATIONS)}')
                continue
            if round_no == '5' and len(parsed) != 2:
                print(f'::warning::R5 needs two fully verified finalist nations; got {sorted(parsed)}')
                continue

            stage = {}
            for country, values in parsed.items():
                roster = by_nation.get(country, [])
                active_names = values['starting'] | values['interchange']
                matched = sum(normalize_name(p['name']) in active_names for p in roster)
                required = max(2, math.ceil(len(roster) * 0.30))
                if not roster or matched < required:
                    print(f'::warning::{country} R{round_no} matches only {matched}/{len(roster)} market players; '
                          f'need {required}; keeping previously verified feed')
                    continue
                stage[country] = {
                    p['id']: ('starting' if normalize_name(p['name']) in values['starting']
                              else 'interchange' if normalize_name(p['name']) in values['interchange']
                              else 'omitted')
                    for p in roster
                }

            if round_no in ('4', '5'):
                # Atomic knockout publication: 140 market players get a status.
                # Eliminated nations (no fixture) are explicitly "omitted".
                if set(stage) != set(parsed):
                    print(f'::warning::R{round_no}: market mapping incomplete. No partial knockout publication.')
                    continue
                complete = {p['id']: 'omitted' for p in players if p.get('marketEligible')}
                for statuses in stage.values():
                    complete.update(statuses)
                result['rounds'][round_no] = complete
            else:
                # Keep earlier group rounds fail-closed too: if a page parses
                # but no nation passes market-roster matching, do not create
                # an empty round key (required by test_update regression).
                if stage:
                    current = result['rounds'].setdefault(round_no, {})
                    for statuses in stage.values():
                        current.update(statuses)

            for country, statuses in stage.items():
                verified.append((round_no, country, len(statuses)))
            if stage:
                newest_published = max(newest_published, int(round_no))
                found = result['sources'].setdefault(round_no, [])
                if not any(x.get('url') == url for x in found):
                    found.append({'url': url, 'label': f'NRL.com World Cup men - round {round_no}'})
                if round_no in ('4', '5'):
                    # Never let an additional, potentially stale page overwrite
                    # a fully verified single-source knockout snapshot.
                    break
    return result,verified


def run(offline_pages=None, round_override=None):
    feed_path=ROOT/'worldcup-teamlists.json'
    roster=json.loads((ROOT/'roster.json').read_text(encoding='utf8'))
    before=json.loads(feed_path.read_text(encoding='utf8'))
    manual=json.loads((ROOT/'additional_sources.json').read_text(encoding='utf8'))
    manual_urls = [u for vals in manual.values() for u in vals]
    today = datetime.now(ZoneInfo('Australia/Sydney')).date()
    target = round_override or current_target_round(today)
    print(f'Team list publisher target: Round {target} (Australia/Sydney {today}); scanning current round only')
    sources=(discover_urls(manual=manual_urls, previous=before, today=today,
                           focus_round=target) if offline_pages is None
             else {r:list(p) for r,p in offline_pages.items()})
    if offline_pages is None and not sources.get(target):
        raise RuntimeError(f'No official article candidate for Round {target}')
    pages={}
    for round_no,urls in sources.items():
        if offline_pages is not None: pages[round_no]=offline_pages[round_no]
        else:
            # Complete fixture cards: 10 nations in R1/R2/R3, four in SF, two in Final.
            expected={'1':10,'2':10,'3':10,'4':4,'5':2}.get(round_no,1)
            for url in urls:
                source = get_official_source(url, expected_nations=expected)
                if not source:
                    continue
                parsed = complete_matchday(parse_teams(source))
                if round_no == '4' and set(parsed) != ROUND_4_NATIONS:
                    print(f'::warning::R4 article is not a fully validated four-nation snapshot: {sorted(parsed)}')
                    continue
                if round_no == '5' and len(parsed) != 2:
                    continue
                pages.setdefault(round_no, {})[url] = source
                if round_no in ('4', '5'):
                    break
    candidate,verified=build_changes(before,roster,pages)
    # A successful scheduled run means CURRENT official team sheets were verified,
    # not merely that a historical/cached JSON entry already exists.
    if offline_pages is None:
        verified_current = {nation for rnd, nation, _ in verified if rnd == target}
        required = ROUND_4_NATIONS if target == '4' else set()
        if target == '4' and verified_current != required:
            raise RuntimeError('Round 4 official verification incomplete: '
                               f'{sorted(verified_current)}; expected {sorted(required)}. '
                               'Existing feed preserved; no stale success reported.')
        if target == '5' and len(verified_current) != 2:
            raise RuntimeError('Round 5 official finalist lists not fully verified; last feed preserved.')
        if target in ('1', '2', '3') and not verified_current:
            raise RuntimeError(f'No new official verification for Round {target}; last feed preserved.')
        expected_ids = {p['id'] for p in roster if p.get('marketEligible')}
        if target in ('4', '5') and set(candidate.get('rounds', {}).get(target, {})) != expected_ids:
            raise RuntimeError(f'Round {target} incomplete market coverage; no publication')
        print(f'::notice::Official R{target} verification PASSED: {len(verified_current)} nations, '
              f'{len(candidate["rounds"][target])} player statuses')
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
    parser.add_argument('--fixture-dir', type=Path, help='Use locally saved pages for offline verification')
    parser.add_argument('--round', dest='round_override', choices=['1','2','3','4','5'],
                        help='Explicit official round target; defaults to current Australia/Sydney round')
    args=parser.parse_args()
    if args.fixture_dir:
        sources={}
        for p in sorted(args.fixture_dir.glob('round[1-5]_*.txt')):
            rd=p.name[5]
            sources.setdefault(rd,{})[f'https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-{rd}/']=p.read_text(encoding='utf8')
        run(sources, round_override=args.round_override)
    else:
        run(round_override=args.round_override)
