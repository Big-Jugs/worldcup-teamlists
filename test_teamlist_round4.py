"""Offline regression tests for verified knockout status publication.

Run: python -m unittest test_teamlist_round4 -v
No NRL traffic is used; constructed input has the same official role/number format.
"""
import importlib
import json
import re
import unittest
from datetime import date
from pathlib import Path

import update_teamlists as mod


PATCH_FILE = Path(__file__).resolve().parent.parent / 'WCF_Round4_Official_Teamlist_Status_Fix_2026-11-05.html'


def sample_rosters():
    # This is a frozen, published Nov 2026 sheet used only as a parser fixture.
    if PATCH_FILE.exists():
        html = PATCH_FILE.read_text(encoding='utf8')
        match = re.search(r'const ROSTERS = (\{.*?\});', html)
        if match:
            return json.loads(match.group(1))
    # Independent small stand-in can be expanded to 22 named men per country.
    return {code: [f'{code} Player {i}' for i in range(1, 23)]
            for code in sorted(mod.ROUND_4_NATIONS)}


def sample_html(squads, exclude=None):
    names = {'NZL': 'New Zealand', 'TON': 'Tonga', 'AUS': 'Australia', 'SAM': 'Samoa'}
    roles = ['Fullback','Winger','Centre','Centre','Winger','Five-Eighth',
             'Halfback','Prop','Hooker','Prop','2nd Row','2nd Row','Lock']
    lines = []
    for code, players in squads.items():
        if code == exclude:
            continue
        for i, name in enumerate(players, 1):
            role = roles[i-1] if i <= 13 else ('Interchange' if i <= 19 else 'Reserve')
            lines.append(f'{role} for {names[code]} is number {i} {name}')
    return '\n'.join(lines)


def make_players(rosters):
    players = []
    counter = 1
    for code, names in rosters.items():
        for name in names:
            players.append({'id': f'p{counter}', 'nation': code,
                            'name': name, 'marketEligible': True})
            counter += 1
    for j in range(140 - len(players)):
        players.append({'id': f'p{counter}', 'nation': 'ENG',
                        'name': f'England no fixture {j}', 'marketEligible': True})
        counter += 1
    return players


class Round4PublisherTests(unittest.TestCase):
    def setUp(self):
        self.squads = sample_rosters()
        self.players = make_players(self.squads)
        self.url = mod.KNOWN_OFFICIAL_URLS['4']
        self.initial = {'schema': 1,
                        'updatedAt': '2026-10-30T09:00:03+00:00',
                        'rounds': {'3': {'p1': 'starting'}},
                        'sources': {'3': [{'url': 'https://www.nrl.com/news/2026/10/27/world-cup-team-lists-mens-round-3/', 'label': 'old'}]}}

    def test_discovery_prioritises_known_official_semifinal_link(self):
        result = mod.discover_urls(previous=self.initial, today=date(2026,11,5),
                                   focus_round='4')
        self.assertEqual(result, {'4': [self.url]})

    def test_round_window(self):
        self.assertEqual(mod.current_target_round(date(2026,11,5)), '4')
        self.assertEqual(mod.current_target_round(date(2026,11,10)), '5')

    def test_realistic_nrl_html_nested_player_cards(self):
        # NRL hides the role/jersey in a visually-hidden span and puts the
        # first/last name in separate sibling text nodes. Previously all
        # these entries were silently missed by visible_lines().
        teams = {'NZL': 'New Zealand', 'TON': 'Tonga',
                 'AUS': 'Australia', 'SAM': 'Samoa'}
        roles = ['Fullback', 'Winger', 'Centre', 'Centre', 'Winger',
                 'Five-Eighth', 'Halfback', 'Prop', 'Hooker', 'Prop',
                 '2nd Row', '2nd Row', 'Lock']
        cards = []
        for nation, players in self.squads.items():
            for index, player in enumerate(players, 1):
                role = roles[index - 1] if index <= 13 else (
                    'Interchange' if index <= 19 else 'Reserve')
                parts = player.split(' ', 1)
                first, surname = (parts[0], parts[1]) if len(parts) > 1 else (player, '')
                cards.append(
                    '<div class="team-list-profile__name">'
                    f'<span class="u-visually-hidden">{role} for {teams[nation]} '
                    f'is number {index}</span> {first}'
                    f' <span class="u-font-weight-700">{surname}</span>'
                    '</div>')
        html = '<html><body>' + ''.join(cards) + '</body></html>'
        parsed = mod.complete_matchday(mod.parse_teams(html))
        self.assertEqual(set(parsed), mod.ROUND_4_NATIONS)
        for value in parsed.values():
            self.assertEqual(len(value['starting']), 13)
            self.assertEqual(len(value['interchange']), 6)

    def test_four_complete_squads_parsed(self):
        parsed = mod.complete_matchday(mod.parse_teams(sample_html(self.squads)))
        self.assertEqual(set(parsed), mod.ROUND_4_NATIONS)
        for value in parsed.values():
            self.assertEqual(len(value['starting']), 13)
            self.assertEqual(len(value['interchange']), 6)

    def test_round4_atomic_full_market_status_and_historical_preservation(self):
        result, verified = mod.build_changes(self.initial, self.players,
                                            {'4': {self.url: sample_html(self.squads)}})
        self.assertEqual(set(result['rounds']['4']), {p['id'] for p in self.players})
        self.assertEqual(len(result['rounds']['4']), 140)
        self.assertEqual(len([x for x in verified if x[0] == '4']), 4)
        self.assertEqual(result['rounds']['3'], self.initial['rounds']['3'])
        self.assertEqual(sum(v == 'starting' for v in result['rounds']['4'].values()), 52)
        self.assertEqual(sum(v == 'interchange' for v in result['rounds']['4'].values()), 24)
        self.assertEqual(sum(v == 'omitted' for v in result['rounds']['4'].values()), 64)

    def test_missing_nation_never_publishes_partial_round(self):
        result, verified = mod.build_changes(self.initial, self.players,
                          {'4': {self.url: sample_html(self.squads, exclude='SAM')}})
        self.assertNotIn('4', result['rounds'])
        self.assertFalse(verified)

    def test_late_changes_rewrite_same_round_atomic_snapshot(self):
        original, _ = mod.build_changes(self.initial, self.players,
                                       {'4': {self.url: sample_html(self.squads)}})
        revised = {k: list(v) for k,v in self.squads.items()}
        revised['NZL'][0], revised['NZL'][14] = revised['NZL'][14], revised['NZL'][0]
        updated, verified = mod.build_changes(original, self.players,
                                             {'4': {self.url: sample_html(revised)}})
        moved_to_bench = self.squads['NZL'][0]
        moved_to_start = self.squads['NZL'][14]
        id_by_name = {(p['nation'], p['name']): p['id'] for p in self.players}
        self.assertEqual(updated['rounds']['4'][id_by_name['NZL', moved_to_bench]], 'interchange')
        self.assertEqual(updated['rounds']['4'][id_by_name['NZL', moved_to_start]], 'starting')
        self.assertEqual(sum(v == 'omitted' for v in updated['rounds']['4'].values()), 64)
        self.assertEqual(len(verified), 4)


if __name__ == '__main__':
    unittest.main()

class FullRunTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.rosters = sample_rosters()
        self.players = make_players(self.rosters)
        (self.path / 'roster.json').write_text(json.dumps(self.players))
        self.initial = {'schema': 1, 'rounds': {'3': {'p1': 'starting'}},
                        'sources': {}, 'updatedAt': '2026-10-30T09:00:03+00:00'}
        (self.path / 'worldcup-teamlists.json').write_text(json.dumps(self.initial))
        (self.path / 'additional_sources.json').write_text('{}')
        self.original_root = mod.ROOT
        mod.ROOT = self.path

    def tearDown(self):
        mod.ROOT = self.original_root
        self.tmp.cleanup()

    def test_publishes_once_then_idempotent(self):
        from unittest.mock import patch
        self.source = sample_html(self.rosters)
        with patch.object(mod, 'discover_urls', return_value={'4':[mod.KNOWN_OFFICIAL_URLS['4']]}), \
             patch.object(mod, 'get_official_source', return_value=self.source):
            self.assertTrue(mod.run(round_override='4'))
            feed = json.loads((self.path / 'worldcup-teamlists.json').read_text())
            self.assertEqual(len(feed['rounds']['4']), 140)
            stamp = feed['updatedAt']
            self.assertFalse(mod.run(round_override='4'))
            feed_again = json.loads((self.path / 'worldcup-teamlists.json').read_text())
            self.assertEqual(feed_again['updatedAt'], stamp)

    def test_failed_official_verification_never_writes(self):
        from unittest.mock import patch
        before = (self.path / 'worldcup-teamlists.json').read_bytes()
        with patch.object(mod, 'discover_urls', return_value={'4':[mod.KNOWN_OFFICIAL_URLS['4']]}), \
             patch.object(mod, 'get_official_source', return_value=sample_html(self.rosters, exclude='AUS')):
            with self.assertRaisesRegex(RuntimeError, 'verification incomplete'):
                mod.run(round_override='4')
        self.assertEqual((self.path / 'worldcup-teamlists.json').read_bytes(), before)
