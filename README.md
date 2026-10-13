# World Cup Fantasy — automatic NRL.com team-list feed

This repository contains a **public player-key data feed**, not your fantasy squad or user data. A GitHub Actions workflow checks official **men's World Cup** team lists on NRL.com every 30 minutes and commits `worldcup-teamlists.json` **only when a complete published match-day list changes**. It does not change users' squads, prices, or scores.

## Connect in 10 minutes (first-time setup)

1. Sign in to [GitHub](https://github.com/) or create a free account.
2. Create a **public** repository named **`worldcup-teamlists`**, with default branch **`main`**. The exact repository name is optional, but use it in your feed URL.
3. Upload the **contents of this folder** to the repository, **preserving the `.github/workflows/refresh-teamlists.yml` path**. Do not upload the ZIP as a single file; GitHub needs the files unpacked. On desktop, GitHub → your repository → **Add file → Upload files** and drag the extracted files/folders. Ensure `roster.json`, `update_teamlists.py`, `requirements.txt`, `additional_sources.json`, `worldcup-teamlists.json`, and `.github/workflows/refresh-teamlists.yml` are present.
4. In the repository's **Actions** tab, enable workflows if GitHub asks. Choose **Refresh World Cup team lists → Run workflow**. It should create a green check. If push is denied, review repository **Settings → Actions → General → Workflow permissions**, and allow read/write permissions where your account permits it.
5. Your public JSON feed will be at:

   `https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/worldcup-teamlists/main/worldcup-teamlists.json`

6. Upload the **separate V7.2 Tiiny Host website ZIP** to your *existing* `nrlworldcupfantasy.tiiny.site` project. Back up your squad with **Scoring & Settings → Export** first.
7. Open the game in Safari → player market → **Connect live updates**. Paste your feed URL → **Save & check feed**. It checks the source every 15 minutes while the app is open and on reload.

You only need to connect the feed **once per browser/device**. Your fantasy squad remains in that browser's local storage. The 140 selectable players, $11m cap, and Round 1 key snapshot remain unchanged.

## How it works

- Official NRL.com men's team-list pages are discovered from NRL.com's news pages and an optional list of explicit sources in `additional_sources.json`.
- Scraper reads the name and role labels for each country's published match-day team.
- It **must find 13 starters and six interchange/bench players for each nation** before changing that nation's player key. On NRL.com's match cards, interchange 14–17 and the first two reserves (18–19) count as six World Cup bench positions. Extended reserves after 19 are *not* interchange.
- For a verified nation's 140 selectable roster subset, status becomes `starting`, `interchange`, or `omitted`. Other countries and any not-yet-published rounds are never guessed.
- When a published list is incomplete, the source is unavailable, or the page layout changes, the workflow logs a warning and **keeps the last valid statuses**, instead of turning unverified players red. Updates normally appear within around 30–60 minutes, depending on GitHub scheduling, raw-content caching, and the game's 15-minute refresh.
- The scheduled workflow normally runs at :17 and :47 UTC each hour. GitHub can delay/drop scheduled jobs or suspend them after 60 days of inactivity. Open the Actions tab if updates seem stale.
- The output JSON contains source links to NRL.com for review. It is publicly readable but has **no saved teams, accounts, or private information**.

## New rounds or parser problems

The updater searches NRL.com news indexes automatically for URLs with names such as `world-cup-team-lists-mens-round-2` or `world-cup-team-lists-mens-semi-finals`. If a new article is missed, add its NRL.com URL to the matching array in `additional_sources.json`, commit, then run the workflow. Future lists are not guaranteed discoverable under those names. **This is a best-effort public-web scraper, not an official NRL API.** HTML layout or anti-bot policy changes may require code maintenance. No website can guarantee official live selections without a supported upstream data feed.

For emergency use, export the JSON file from this repo or edit it manually and use the in-game **Import** function; this does not change fantasy-squad data.

## Testing

Run `python -m pip install -r requirements.txt`, then `python -m unittest discover -s tests -v`. `python update_teamlists.py` runs a real check (internet required). GitHub Actions runs it unattended.

Source: [NRL.com World Cup men's Round 1 team lists](https://www.nrl.com/news/2026/10/13/world-cup-team-lists-mens-round-1/). This is an unofficial fan tool and is not affiliated with the NRL.
