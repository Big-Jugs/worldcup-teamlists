import sys,unittest,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'github'))
from update_teamlists import parse_teams,complete_matchday,build_changes,normalize_name,round_for_url


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

if __name__=='__main__':unittest.main()
