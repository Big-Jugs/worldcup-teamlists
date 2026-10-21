import sys,unittest,json
from datetime import date
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'github'))
from update_teamlists import parse_teams,complete_matchday,build_changes,normalize_name,round_for_url,discover_urls,prospective_urls


def markup(team='Australia', include_bench=True, style='html'):
    lines=[]
    startroles=['Fullback','Winger','Centre','Centre','Winger','Five-Eighth','Halfback','Prop','Hooker','Prop','2nd Row','2nd Row','Lock']
    for i,role in enumerate(startroles,1):lines.append(f'{role} for {team} is number {i} Test Player {i}')
    if include_bench:
        for i in range(14,18):lines.append(f'Interchange for {team} is number {i} Test Player {i}')
        for i in range(18,23):lines.append(f'Reserve for {team} is number {i} Test Player {i}')
    if style=='html':return '<html><body>'+''.join(f'<img alt="{line}" src="x" />' for line in lines)+'</body></html>'
    return '\n'.join('* '+line for line in lines)

class ParserTests(unittest.TestCase):
    def test_html_and_markdown_nineteen_only(self):
        for style in ('html','md'):
            with self.subTest(style=style):
                roster=complete_matchday(parse_teams(markup(style=style)))
                self.assertEqual(len(roster['AUS']['starting']),13)
                self.assertEqual(len(roster['AUS']['interchange']),6)
                self.assertIn('testplayer18',roster['AUS']['interchange'])
                self.assertIn('testplayer19',roster['AUS']['interchange'])
                self.assertNotIn('testplayer20',roster['AUS']['interchange'])
    def test_incomplete_nation_does_not_mark_omitted(self):
        people=[{'id':'p'+str(i),'name':f'Test Player {i}','nation':'AUS','marketEligible':True} for i in range(1,24)]
        before={'schema':1,'updatedAt':'2026-10-13T05:59:00Z','rounds':{'1':{'p1':'starting','p20':'omitted'}},'sources':{'1':[]}}
        pages={'1':{'https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-1/':markup(include_bench=False)}}
        nxt,valid=build_changes(before,people,pages)
        self.assertEqual(nxt,before)
        self.assertEqual(valid,[])
    def test_complete_nation_marks_start_bench_and_excluded(self):
        people=[{'id':'p'+str(i),'name':f'Test Player {i}','nation':'AUS','marketEligible':True} for i in range(1,24)]
        before={'schema':1,'updatedAt':'2026-10-13T05:59:00Z','rounds':{'1':{}},'sources':{}}
        pages={'1':{'https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-1/':markup()}}
        nxt,valid=build_changes(before,people,pages)
        self.assertEqual(len(valid),1)
        self.assertEqual(nxt['rounds']['1']['p1'],'starting')
        self.assertEqual(nxt['rounds']['1']['p19'],'interchange')
        self.assertEqual(nxt['rounds']['1']['p20'],'omitted')
    def test_unknown_names_cannot_turn_a_country_red(self):
        people=[{'id':'p'+str(i),'name':f'Actual Star {i}','nation':'AUS','marketEligible':True} for i in range(1,24)]
        before={'schema':1,'updatedAt':'2026-10-13T05:59:00Z','rounds':{'1':{'p1':'starting'}},'sources':{}}
        pages={'1':{'https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-1/':markup()}}
        nxt,valid=build_changes(before,people,pages)
        self.assertEqual(nxt,before)
        self.assertFalse(valid)
    def test_name_normalization_and_future_round(self):
        self.assertEqual(normalize_name('Sualauvi Fa’alogo'),normalize_name("Sua Fa'alogo"))
        self.assertEqual(round_for_url('https://www.nrl.com/news/2026/10/20/world-cup-team-lists-mens-round-2/'),'2')
        self.assertEqual(round_for_url('https://www.nrl.com/news/2026/11/03/world-cup-team-lists-mens-semi-finals/'),'4')
        self.assertEqual(round_for_url('https://www.nrl.com/news/2026/10/20/world-cup-team-lists-womens-round-2/'),None)



# 19 Tonga players transcribed from the published men's Round 2 NRL.com article.
# The official article uses these same `Role for Nation is number N Name`
# descriptions, including jersey numbers 14-19 all labelled Interchange.
R2_TONGA_START = [
    'Lehi Hopoate', 'Sione Katoa', 'Tolutau Koula', 'Paul Alamoti',
    'Jojo Fifita', 'Tuimoala Lolohea', 'Isaiya Katoa', 'Addin Fonua-Blake',
    'Salesi Foketi', 'Stefano Utoikamanu', "Haumole Olakau'atu",
    'Kulikefu Finefeuiaki', 'Jason Taumalolo',
]
R2_TONGA_BENCH = ['Soni Luke', 'Taniela Paseka', 'Demitric Vaimauga',
                  'Leka Halasima', 'Latu Fainu', 'Dallin Watene-Zelezniak']


def official_r2_tonga_fixture():
    positions = ['Fullback', 'Winger', 'Centre', 'Centre', 'Winger',
                 'Five-Eighth', 'Halfback', 'Prop', 'Hooker', 'Prop',
                 '2nd Row', '2nd Row', 'Lock']
    lines = [f'{role} for Tonga is number {i} {name}'
             for i, (role, name) in enumerate(zip(positions,R2_TONGA_START),1)]
    lines += [f'Interchange for Tonga is number {i} {name}'
              for i,name in enumerate(R2_TONGA_BENCH,14)]
    return '<html><body>' + ''.join(f'<img alt="{line}" />' for line in lines) + '</body></html>'


class MultiRoundAutomationTests(unittest.TestCase):
    R2_URL = 'https://www.nrl.com/news/2026/10/20/world-cup-team-lists-mens-round-2/'

    def test_round2_fixture_is_publishable_and_does_not_change_round1(self):
        roster = json.loads((Path(__file__).parent/'roster.json').read_text())
        before = {'schema':1,'updatedAt':'2026-10-13T16:59:00+11:00',
                  'rounds':{'1':{'p225':'starting','p219':'starting'}},
                  'sources':{'1':[]}}
        after, verified = build_changes(before,roster,
                                        {'2':{self.R2_URL:official_r2_tonga_fixture()}})
        self.assertEqual(after['rounds']['1'], before['rounds']['1'])
        self.assertEqual(after['rounds']['2']['p225'],'interchange')  # Soni Luke
        self.assertEqual(after['rounds']['2']['p211'],'starting')  # Haumole
        self.assertEqual(after['rounds']['2']['p219'],'omitted')  # Moeaki
        self.assertEqual(after['rounds']['2']['p228'],'starting')  # Tolutau
        self.assertEqual(len([x for x in roster if x.get('marketEligible') and x['nation']=='TON']),
                         sum(p['id'] in after['rounds']['2'] for p in roster if p['nation']=='TON'))
        self.assertIn(('2','TON',21),verified)
        self.assertEqual(after['sources']['2'][0]['url'],self.R2_URL)
        same, verified_again = build_changes(after,roster,
                                        {'2':{self.R2_URL:official_r2_tonga_fixture()}})
        self.assertEqual(after,same)
        self.assertEqual(len(verified_again),1)

    def test_dont_create_new_round_when_no_nation_matches_roster(self):
        before={'schema':1,'updatedAt':'2026-10-13','rounds':{'1':{'p1':'starting'}},'sources':{'1':[]}}
        people=[{'id':'p'+str(i),'name':f'Unrelated Player {i}','nation':'TON','marketEligible':True}
                for i in range(1,24)]
        result,verified=build_changes(before,people,{'2':{self.R2_URL:official_r2_tonga_fixture()}})
        self.assertFalse(verified)
        self.assertNotIn('2',result['rounds'])

    def test_future_dates_are_not_guessed_before_publication(self):
        self.assertEqual(prospective_urls('3',date(2026,10,21)),[])
        self.assertEqual(prospective_urls('4',date(2026,10,21)),[])
        self.assertEqual(prospective_urls('5',date(2026,10,21)),[])
        self.assertEqual(prospective_urls('3',date(2026,10,27)),[
            'https://www.nrl.com/news/2026/10/27/world-cup-team-lists-mens-round-3/'])

    def test_discovers_round2_even_when_nrl_index_omits_it(self):
        with patch('update_teamlists.download_text',return_value='<html><body>No relevant links</body></html>'):
            links=discover_urls(manual=[self.R2_URL],today=date(2026,10,21))
        self.assertIn(self.R2_URL,links['2'])
        self.assertNotIn('3',links)
        self.assertNotIn('4',links)

    def test_automatic_round3_and_final_fallback_when_index_omits_links(self):
        with patch('update_teamlists.download_text',return_value='<html><body>No relevant links</body></html>'):
            round3=discover_urls(today=date(2026,10,28))
            finals=discover_urls(today=date(2026,11,11))
        self.assertIn('3',round3)
        self.assertTrue(any('2026/10/27/' in link for link in round3['3']))
        self.assertTrue(any('semi-finals' in link for link in finals['4']))
        self.assertTrue(any('mens-final/' in link for link in finals['5']))

    def test_published_old_round_is_frozen_after_next_round_exists(self):
        roster = json.loads((Path(__file__).parent/'roster.json').read_text())
        before={'schema':1,'updatedAt':'2026-10-21',
                'rounds':{'1':{'p225':'omitted'},'2':{'p225':'starting'}},
                'sources':{'1':[],'2':[]}}
        old_page=official_r2_tonga_fixture()
        changed,verified=build_changes(before,roster,
           {'1':{'https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-1/':old_page}})
        self.assertEqual(changed,before)
        self.assertEqual(verified,[])

    def test_previous_verified_source_stays_in_discovery(self):
        with patch('update_teamlists.download_text',return_value='<html><body>No relevant links</body></html>'):
            found=discover_urls(previous={'sources':{'2':[{'url':self.R2_URL}]}},today=date(2026,10,21))
        self.assertIn(self.R2_URL,found['2'])


if __name__=='__main__':unittest.main()



class NetworkFallbackTests(unittest.TestCase):
    class Reply:
        status_code=200
        def __init__(self,source):
            self.text=source
            self.content=source.encode('utf-8')
        def raise_for_status(self): pass

    class FakeSession:
        def __init__(self, responses):
            self.responses=iter(responses)
            self.calls=[]
        def get(self,url,**kwargs):
            self.calls.append(url)
            return NetworkFallbackTests.Reply(next(self.responses))

    def test_http_200_without_players_triggers_readable_fallback(self):
        from update_teamlists import get_official_source
        source='<!doctype html><html><body>'+'Loading team list...'*30+'</body></html>'
        markdown=markup(style='md').replace('Fullback for Australia is number 1 Test Player 1',
           '[Fullback for Australia is number 1 Test Player 1](https://www.nrl.com/)')
        session=self.FakeSession([source,markdown])
        def browser(_): raise AssertionError('browser fallback should not run')
        result=get_official_source(MultiRoundAutomationTests.R2_URL,session=session,
                                   expected_nations=1,render=browser)
        self.assertEqual(len(complete_matchday(parse_teams(result))),1)
        self.assertEqual(len(session.calls),2)
        self.assertIn('r.jina.ai',session.calls[1])

    def test_both_nonextractable_sources_trigger_browser(self):
        from update_teamlists import get_official_source
        bad='<html><body>'+'Team list loading...'*50+'</body></html>'
        session=self.FakeSession([bad,bad])
        calls=[]
        def browser(url):
            calls.append(url)
            return markup(style='html')
        got=get_official_source(MultiRoundAutomationTests.R2_URL,session=session,
                                expected_nations=1,render=browser)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(complete_matchday(parse_teams(got))),1)

    def test_all_unverifiable_sources_publish_nothing(self):
        from update_teamlists import get_official_source
        bad='<html><body>'+'Team list loading...'*50+'</body></html>'
        got=get_official_source(MultiRoundAutomationTests.R2_URL,
             session=self.FakeSession([bad,bad]),expected_nations=1,render=lambda _:bad)
        self.assertEqual(got,'')

    def test_reserve_20_does_not_fill_missing_bench_player(self):
        bad=markup().replace('Interchange for Australia is number 17 Test Player 17','')
        result=complete_matchday(parse_teams(bad))
        self.assertNotIn('AUS',result)

    def test_official_interchange_role_handles_jersey_number_swaps(self):
        shifted=markup().replace('Halfback for Australia is number 7 Test Player 7',
                                 'Halfback for Australia is number 17 Test Player 7')
        shifted=shifted.replace('Interchange for Australia is number 17 Test Player 17',
                                'Interchange for Australia is number 7 Test Player 17')
        self.assertEqual(len(complete_matchday(parse_teams(shifted))['AUS']['interchange']),6)

    def test_newer_round_verified_before_existing_old_round_is_changed(self):
        from update_teamlists import build_changes
        people=[{'id':'p'+str(i),'name':f'Test Player {i}','nation':'AUS','marketEligible':True}
                for i in range(1,24)]
        before={'schema':1,'updatedAt':'old','rounds':{'1':{'p1':'omitted'}},'sources':{}}
        after,valid=build_changes(before,people,{'1':{'old-r1':markup()},'2':{'new-r2':markup()}})
        self.assertEqual(after['rounds']['1'],before['rounds']['1'])
        self.assertEqual(len(valid),1)
        self.assertEqual(after['rounds']['2']['p1'],'starting')


if __name__ == '__main__':
    unittest.main()

class PairedTeamColumnTests(unittest.TestCase):
    """Regression for Oct 21 production failure: 5 apparent teams, 0 matches.

    The readable NRL article flattens its two-column table into strings like
    'Fullback for Tonga is number 1 Lehi Hopoate 1 Fullback for Lebanon is
     number 1 Jacob Kiraz'. These MUST yield two independent player records.
    """
    POSITIONS = ['Fullback','Winger','Centre','Centre','Winger','Five-Eighth',
                 'Halfback','Prop','Hooker','Prop','2nd Row','2nd Row','Lock'] + ['Interchange']*6

    def paired_page(self, nations):
        roster = json.loads((Path(__file__).parent/'roster.json').read_text())
        parts=[]
        for a,b in nations:
            players={}
            for nation in (a,b):
                names=[p['name'] for p in roster if p.get('marketEligible') and p['nation']==nation]
                names=(names+[f'Nonmarket {nation} Player {chr(65+i)}' for i in range(20)])[:19]
                players[nation]=names
            for i,role in enumerate(self.POSITIONS,1):
                # Simulate markdown's collapsed opponent columns and the
                # interposed jersey number(s). Opposite column may be different.
                number_b = 17 if b=='COK' and i==7 else 7 if b=='COK' and i==17 else i
                parts.append(f'* {role} for {self.country(a)} is number {i} {players[a][i-1]} '
                             f'{i} {role} for {self.country(b)} is number {number_b} {players[b][i-1]}')
            # Reserve jersey 20+ must never be counted in the 19.
            parts.append(f'Reserve for {self.country(a)} is number 20 Reserve {a} '
                         f'20 Reserve for {self.country(b)} is number 20 Reserve {b}')
        return '\n'.join(parts)

    @staticmethod
    def country(code):
        return {'TON':'Tonga','LEB':'Lebanon','PNG':'Papua New Guinea','SAM':'Samoa',
                'ENG':'England','FRA':'France','NZL':'New Zealand',
                'COK':'Cook Islands','AUS':'Australia','FIJ':'Fiji'}[code]

    def test_exact_real_tonga_lebanon_line(self):
        parsed=parse_teams('* Fullback for Tonga is number 1 Lehi Hopoate 1 '
                           'Fullback for Lebanon is number 1 Jacob Kiraz')
        self.assertEqual(parsed['TON']['start'][0][0],'lehihopoate')
        self.assertEqual(parsed['LEB']['start'][0][0],'jacobkiraz')
        self.assertNotIn('lebanon',parsed['TON']['start'][0][0])

    def test_all_ten_nations_complete_from_paired_markdown(self):
        matchups=[('TON','LEB'),('PNG','SAM'),('ENG','FRA'),('NZL','COK'),('AUS','FIJ')]
        official_page=self.paired_page(matchups)
        parsed=complete_matchday(parse_teams(official_page))
        self.assertEqual(set(parsed), {x for m in matchups for x in m})
        self.assertEqual(len(parsed['TON']['starting']),13)
        self.assertEqual(len(parsed['TON']['interchange']),6)
        self.assertNotIn('reserveton',parsed['TON']['interchange'])
        roster=json.loads((Path(__file__).parent/'roster.json').read_text())
        before={'schema':1,'updatedAt':'before','rounds':{'1':{'p1':'starting'}},'sources':{'1':[]}}
        url='https://www.nrl.com/news/2026/10/20/world-cup-team-lists-mens-round-2/'
        updated, verified=build_changes(before,roster,{'2':{url:official_page}})
        self.assertEqual(len(verified),10)
        self.assertEqual(len(updated['rounds']['2']),140)
        self.assertEqual(updated['rounds']['1'],before['rounds']['1'])
        second, verified2=build_changes(updated,roster,{'2':{url:official_page}})
        self.assertEqual(second,updated)
        self.assertEqual(len(verified2),10)

    def test_single_column_and_multiple_jersey_separator(self):
        row=('Interchange for New Zealand is number 14 Te Maire Martin '
             '14 7 Interchange for Cook Islands is number 7 Esan Marsters')
        parsed=parse_teams(row)
        self.assertEqual(parsed['NZL']['bench'][0][0],'temairemartin')
        self.assertEqual(parsed['COK']['bench'][0][0],'esanmarsters')
        single=parse_teams('Prop for France is number 10 Julian Bousquet')
        self.assertEqual(single['FRA']['start'][0][0],'julianbousquet')

    def test_missing_first_team_player_does_not_still_validate(self):
        page=self.paired_page([('TON','LEB')]);page=page.replace('Sione Katoa','',1)
        parsed=complete_matchday(parse_teams(page))
        self.assertNotIn('TON', parsed)
        self.assertIn('LEB', parsed)
