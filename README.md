# NBA Matchup Board

Tonight's NBA starting lineups, injuries, odds and refs (from Rotowire's public lineups page) next to each player's last 10 games (from StatMuse), on one phone-friendly page.

**Live board:** https://ian-proefriedt.github.io/nba-matchup-board/

On a phone, open the link and use **Add to Home Screen** so it opens like an app.

## How it updates

A GitHub Actions workflow runs `build.py` every day at 21:00 UTC (5 PM EDT / 4 PM EST). It writes `site/data.json` and publishes `site/` to GitHub Pages.

To refresh by hand (for example after late lineup news): **Actions → Build board → Run workflow**.

## Run locally

```bash
pip install -r requirements.txt
python build.py
python -m http.server -d site 8000
```

Then open http://localhost:8000.

## Notes

- Minutes are actual minutes from each player's last 10 (or 5) games, not projections. Rotowire's projected minutes are subscriber-only and aren't used.
- StatMuse is only queried for starters and players listed Ques/Doubt, with a short pause between requests.
- If Rotowire or StatMuse change their page layout, `build.py` will need updating.
