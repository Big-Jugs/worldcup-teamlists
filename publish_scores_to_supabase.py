#!/usr/bin/env python3
"""Securely publish verified match fantasy data to the shared Supabase ladder.

Runs only in a trusted GitHub Actions job on the repository main branch.
It never exposes the service-role credential to browser users.
"""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
SCORES_FILE = ROOT / 'worldcup-fantasy-scores.json'


class PublishError(RuntimeError):
    pass


def rpc(url, token, name, payload=None, opener=urlopen):
    """PostgREST RPC using a server-only admin key. Never print the credential."""
    endpoint = url.rstrip('/') + '/rest/v1/rpc/' + name
    headers = {'apikey': token, 'Content-Type': 'application/json', 'Accept': 'application/json'}
    # Modern sb_secret_* keys are NOT JWTs; use only the apikey header.
    # The legacy service_role JWT can also be passed as Authorization.
    if token.startswith('eyJ'):
        headers['Authorization'] = 'Bearer ' + token
    request = Request(endpoint, data=json.dumps(payload or {}).encode('utf-8'), method='POST', headers=headers)
    try:
        with opener(request, timeout=35) as response:
            content = response.read().decode('utf-8')
            result = json.loads(content) if content else None
            return result
    except HTTPError as err:
        # Only print error type and HTTP status. Never expose private API configuration.
        raise PublishError(f'{name}: HTTP {err.code}, check SQL setup and GitHub secrets') from err
    except (URLError, OSError, ValueError) as err:
        raise PublishError(f'{name}: Could not contact Supabase ({type(err).__name__})') from err


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
            count += 1
    return count


def publish(url, token, data, opener=urlopen):
    count = validate_feed(data)
    # Locks are checked every run (including before first fantasy score appears).
    closed = rpc(url, token, 'wcf_lock_due_rounds', opener=opener)
    print(f'Expired rounds locked: {closed}')
    if count == 0:
        print('No verified match scores yet; public ladder unchanged')
        return {'snapshots_checked': 0}
    out = rpc(url, token, 'wcf_publish_scores', {'p_feed': data}, opener=opener)
    if not isinstance(out, dict) or out.get('success') is not True:
        raise PublishError('Supabase did not confirm score publication')
    print(f'Supabase score snapshots checked: {out.get("snapshots_checked", "unknown")}')
    # Only publish summary. Never print credentials, scores, emails or private lineups.
    return out


def main():
    url = os.environ.get('WCF_SUPABASE_URL', '').strip()
    token = os.environ.get('WCF_SUPABASE_SECRET_KEY', '').strip()
    if not url.startswith('https://') or '.supabase.co' not in url:
        raise PublishError('Set GitHub secret WCF_SUPABASE_URL to your Supabase project HTTPS URL')
    if not token:
        raise PublishError('Set GitHub secret WCF_SUPABASE_SECRET_KEY (never add it to game files)')
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
