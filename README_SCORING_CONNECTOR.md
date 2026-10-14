# World Cup Fantasy V8.8 — NRL.com match fantasy score connector

**Status: ready for a real historical-match probe; not yet verified as a live World Cup scoring source.** No changes have been made to Tiiny Host or your GitHub repository by this package.

## What this changes

The existing V8.7/V8.8 HTML already requests this public GitHub file:

`https://raw.githubusercontent.com/Big-Jugs/worldcup-teamlists/main/worldcup-fantasy-scores.json`

This package adds a **server-side publisher** to the existing `Big-Jugs/worldcup-teamlists` repo. Its job is to look for the official NRL match-specific `fantasyPointsTotal` stat on the relevant match page and publish only reliably matched live/final scores. It understands the documented NRL match-centre data structure in which a `match` contains each team's player-ID-to-name lookup, while `players.homeTeam[]` and `players.awayTeam[]` contain stats with matching `playerId` values. It can also inspect JSON fetched by the JavaScript match centre in Chromium.

The publisher will **never substitute season averages, rugby scoreboard points, or fabricated values** for Fantasy scores. If a score source is unavailable, it leaves the prior JSON untouched; existing scores remain visible, but V8.7/V8.8 removes stale live highlighting.

## Exactly what to upload to GitHub

Your public repository already has `roster.json` and other team-list files. **Do not remove them.** Upload these four files from the ZIP to the root of `worldcup-teamlists`:

- `refresh_fantasy_scores.py`
- `score_sources.json`
- `worldcup-fantasy-scores.json` — currently an empty scores file; no invented scores.
- `test_fantasy_scores.py`

And **create one workflow file at exactly** `.github/workflows/refresh-fantasy-scores.yml`. From GitHub's **Code → Add file → Create new file**, type the entire path, then paste the contents of the bundled workflow YAML. This is the same folder technique used to get the team-list updater working.

If you already deployed an earlier experimental version of `worldcup-fantasy-scores.json` containing real scores, BACK IT UP before uploading: do not overwrite official data with the empty starter file.

## Validate the 2025 England v Kangaroos fixture before relying on scores

1. Open `https://github.com/Big-Jugs/worldcup-teamlists/actions`.
2. Select **Refresh World Cup fantasy scores** and click **Run workflow**.
3. Tick/set `probe_2025 = true` and run it.
4. Open the completed run → **refresh** job → **Historical 2025 match probe (non-publishing)**.
5. Check for `Historical match found` with the number of 2026-roster players matched and samples of their Fantasy points. Compare at least two values manually with [England v Kangaroos, 2025](https://www.nrl.com/draw/internationals/2025/game-1/england-v-kangaroos/), **Player Stats → Fantasy**.
6. If the run fails with `Historical probe inconclusive`, save the job log and return it to ChatGPT so the parser can be adapted to the actual NRL payload. A green GitHub workflow check alone is **not** proof that real scores are being retrieved.

Historical mode **does not write** any scores. When this check works, the scheduled workflow will check 2026 World Cup fixture pages around their match dates and publish only verified match-scoped points. The exact NRL World Cup stats format still requires a first-match verification on **15 October 2026**.

## Timing and restrictions

- The GitHub workflow runs approximately every **10 minutes**, but schedule runs can be delayed by GitHub; the browser checks the published JSON every **60 seconds**. This is near-live, not continuous real-time streaming.
- Matches not near their scheduled fixture date are skipped. Published final points remain, and amendments can be ingested for a short post-match window.
- If the official NRL website removes public access, changes its match-centre JSON, or blocks GitHub/automated browsers, this method stops rather than inventing points. A licensed stats provider or authorised API would be the reliable alternative.
- Round 4 semi-finals and Round 5 final have **TBA opponents** and are not yet in `score_sources.json`; add verified fixtures when announced.
- The public multiplayer ladder remains **Supabase-controlled**. This publishes *provisional player points* only; a separate trusted, server-side score-settlement system is still required for the ladder.
- Confirm permitted data usage and source attribution for a public website. Do not copy images or bypass site access controls.

## Local test

Run `python test_fantasy_scores.py` inside the extracted folder (with `roster.json` in the same directory, copied from your existing team-list repo). The tests use documented NRL JSON structures with **fictional numbers only**; no fake points are published. `python refresh_fantasy_scores.py --probe-historical` performs the real read and requires public web access plus installed Playwright/Chromium.
