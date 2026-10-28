#!/usr/bin/env python3
"""Securely publish verified match fantasy data to the shared Supabase ladder.

Runs only in a trusted GitHub Actions job on the repository main branch.
It never exposes the service-role credential to browser users.
"""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
SCORES_FILE = ROOT / 'worldcup-fantasy-scores.json'


class PublishError(RuntimeError):
    pass


def validate_connection_settings(url, token):
    """Reject malformed configuration before placing any credential on the wire."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        valid = (parts.scheme == 'https'
                 and host.endswith('.supabase.co')
                 and host.count('.') == 2
                 and bool(host.partition('.')[0])
                 and parts.username is None and parts.password is None
                 and parts.port is None
                 and parts.path in ('', '/')
                 and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise PublishError('WCF_SUPABASE_URL is malformed. Paste only the Project URL, like https://project-ref.supabase.co (no variable name, quotes or /rest/v1 suffix).')
    if not token:
        raise PublishError('WCF_SUPABASE_SECRET_KEY is empty. Add your project secret key in GitHub Actions secrets.')
    if any(x.isspace() for x in token) or any(x in token for x in ('"', "'")) or (token.startswith('sb_secret_') and '=' in token):
        raise PublishError('WCF_SUPABASE_SECRET_KEY contains whitespace, quotes or an assignment; re-enter just the raw key value.')
    if not (token.startswith('sb_secret_') or token.startswith('eyJ')):
        raise PublishError('WCF_SUPABASE_SECRET_KEY must contain the secret key value only (sb_secret_...), not its name, a publishable key or extra text.')


def rpc(url, token, name, payload=None, opener=urlopen):
    """Call a trusted Supabase RPC and give safe, actionable diagnostics."""
    endpoint = url.rstrip('/') + '/rest/v1/rpc/' + name
    headers = {'apikey': token, 'Content-Type': 'application/json',
               'Accept': 'application/json',
               'User-Agent': 'WorldCupFantasyServerPublisher/8.14'}
    # sb_secret_* keys are not JWTs. Legacy JWT service_role may be Bearer.
    if token.startswith('eyJ'):
        headers['Authorization'] = 'Bearer ' + token
    try:
        request = Request(endpoint, data=json.dumps(payload or {}).encode('utf-8'),
                          method='POST', headers=headers)
        with opener(request, timeout=35) as response:
            status = response.getcode() if hasattr(response, 'getcode') else 200
            content_type = (response.headers.get('Content-Type', '')
                            if hasattr(response, 'headers') else '')
            raw = response.read()
            # RPCs must return JSON (including numeric 0 for lockout RPC).
            try:
                result = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # Do not print the response body: it may include sensitive values.
                kind = 'HTML' if b'<html' in raw[:2048].lower() or 'text/html' in content_type.lower() else 'non-JSON'
                raise PublishError(
                    f'{name}: HTTP {status} returned {kind} instead of JSON. '
                    'Check the Supabase Project URL secret; it must be the API project host, not a dashboard or website URL.'
                ) from exc
            return result
    except HTTPError as err:
        # Never print bodies, headers, URLs or keys.
        raise PublishError(
            f'{name}: Supabase returned HTTP {err.code}. '
            + ('Check WCF_SUPABASE_SECRET_KEY belongs to this project.' if err.code == 401 else
               'Check the SQL functions, access grants, and key permissions.' if err.code in (403, 404) else
               'Check the Supabase API logs for this request.')
        ) from err
    except PublishError:
        raise
    except (URLError, OSError) as err:
        raise PublishError(f'{name}: Network connection to Supabase failed ({type(err).__name__}).') from err
    except ValueError as err:
        raise PublishError(
            f'{name}: Request configuration rejected ({type(err).__name__}). '
            'Check the GitHub secrets for a correctly formatted Project URL and a raw sb_secret_ key (no quotes, prefixes or line breaks).'
        ) from err


def validate_feed(data):
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise PublishError('Unexpected score feed schema; refusing to publish')
    if not isinstance(data.get('rounds'), dict):
        raise PublishError('Score feed rounds must be an object')
    count = 0
    for rn, rd in data['rounds'].items():
        if rn not in ('1', '2', '3', '4', '5') or not isinstance(rd, dict):
            raise PublishError('Unexpected competition round')
        matches = rd.get('matches')
        if not isinstance(matches, dict):
            raise PublishError('Missing fixture dictionary')
        for mid, match in matches.items():
            if not isinstance(mid, str) or not isinstance(match, dict):
                raise PublishError('Invalid fixture')
            if match.get('status') not in ('live', 'final') or not isinstance(match.get('players'), dict):
                raise PublishError('Unverified match state or missing player scores')
            if match.get('status') == 'final':
                cover = match.get('coverage')
                if not isinstance(cover, dict) or cover.get('complete') is not True or cover.get('official_teams') is not True:
                    raise PublishError('Final fixture missing complete official player coverage')
                if cover.get('home_code') != match.get('home') or cover.get('away_code') != match.get('away'):
                    raise PublishError('Official player coverage fixture nations mismatch')
                for side in ('home','away'):
                    c = cover.get(side)
                    if (not isinstance(c, dict) or not isinstance(c.get('rows'), int) or c['rows'] < 19
                        or not isinstance(c.get('identified'), int) or c['identified'] != c['rows']
                        or not isinstance(c.get('appeared'), int) or not 13 <= c['appeared'] <= c['rows']
                        or c.get('unresolved') != 0 or c.get('fantasy_appearances') != c['appeared']):
                        raise PublishError('Final fixture official player coverage incomplete')
                    ids = c.get('eligible_score_ids')
                    if not isinstance(ids, list) or any(not isinstance(pid, str) or not pid for pid in ids):
                        raise PublishError('Final fixture missing matched fantasy player ID evidence')
                ids = cover['home']['eligible_score_ids'] + cover['away']['eligible_score_ids']
                if len(ids) != len(set(ids)) or set(ids) != set(match['players']):
                    raise PublishError('Final fixture fantasy players differ from verified official coverage')
            count += 1
    return count


def publish(url, token, data, opener=urlopen):
    """All scoring, finality, pricing and next-round opening are server-owned.

    GitHub can retry this safely: settled rounds and prices have SQL unique keys.
    """
    count = validate_feed(data)
    closed = rpc(url, token, 'wcf_lock_due_rounds', opener=opener)
    print(f'Rounds locked after last kickoff: {closed}')
    result = {'snapshots_checked': 0}
    if count:
        result = rpc(url, token, 'wcf_publish_scores', {'p_feed': data}, opener=opener)
        if not isinstance(result, dict) or result.get('success') is not True:
            raise PublishError('Supabase did not confirm score publication')
        print(f'Supabase score snapshots checked: {result.get("snapshots_checked", "unknown")}')
    else:
        print('No verified match scores yet; ladder unchanged')
    proof_file = ROOT / 'wcf_source_proofs_runtime.json'
    if not proof_file.exists():
        raise PublishError('Missing live NRL score verification sidecar from this GitHub run')
    proofs = json.loads(proof_file.read_text(encoding='utf-8'))
    proof_list = proofs.get('proven', [])
    if not isinstance(proof_list, list):
        raise PublishError('Invalid source proof file')
    verified = rpc(url, token, 'wcf_attest_recent_source', {'p_verified': proof_list}, opener=opener)
    print('Final match score snapshots freshly verified against NRL.com: ' + str(verified))
    # Server checks 16h after the last observed FULL-TIME, verified completeness,
    # recent source checks and score stability. It adjusts player prices ONCE,
    # finalises the ladder, and only then opens R2/R3 for trades.
    settlement = rpc(url, token, 'wcf_settle_due_rounds', opener=opener)
    if not isinstance(settlement, dict) or settlement.get('success') is not True:
        raise PublishError('Supabase did not confirm the round settlement check')
    print('Settled rounds: ' + str(settlement.get('settled_rounds', [])))
    print('Player prices changed: ' + str(settlement.get('revised_player_prices', 0)))
    return {'snapshots_checked': result.get('snapshots_checked', 0), 'settlement': settlement}

def main():
    url = os.environ.get('WCF_SUPABASE_URL', '').strip()
    token = os.environ.get('WCF_SUPABASE_SECRET_KEY', '').strip()
    validate_connection_settings(url, token)
    if not SCORES_FILE.is_file():
        raise PublishError('Missing worldcup-fantasy-scores.json at repository root')
    data = json.loads(SCORES_FILE.read_text(encoding='utf-8'))
    publish(url, token, data)
    print('Trusted public ladder publisher completed')


if __name__ == '__main__':
    try:
        main()
    except PublishError as exc:
        print('ERROR: ' + str(exc))
        raise SystemExit(1)
