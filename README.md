# NBA Matchup Board

Tonight's NBA starting lineups, injuries, odds and refs (from Rotowire's public lineups page) next to each player's last 10 games (from StatMuse), on one phone-friendly page, with suggested player prop bets and buttons that open each prop at FanDuel or DraftKings.

**Live board:** https://ian-proefriedt.github.io/nba-matchup-board/

On a phone, open the link and use **Add to Home Screen** so it opens like an app.

## How it updates

A GitHub Actions workflow runs `build.py` every day at 21:00 UTC (5 PM EDT / 4 PM EST). It writes `site/data.json` and publishes `site/` to GitHub Pages.

To refresh by hand (for example after late lineup news): **Actions → Build board → Run workflow**.

## Suggested bets

For every points, rebounds, assists and threes line at FanDuel and DraftKings, `build.py` (`suggest()`) estimates the chance of the over:

1. **Expected stat**: a blend of FanDuel Research's projection, his per-minute rate over the last 10 games times tonight's projected minutes, and a recent average where the last 5 games count double.
2. **Probability**: a normal curve around that expectation (Poisson for threes), averaged with his recent hit rate on that exact line (shrunk toward 50%).
3. **Against the market**: that probability is pulled 60% of the way back to the book's no-vig price, since books are usually right. A bet is suggested only when there's still value left: **Strong** = 10%+ expected profit per dollar, **Lean** = 4%+. Both books are checked, and the board recommends whichever line and price is better.

There are no picks once a game tips off, because Rotowire then shows live lines. There are also none when the model and the book disagree by more than 22 points (usually a stale line or a data mismatch). Players listed questionable are capped at Lean, with a warning. Each pick lists the reasons behind it: hit rate, averages, projection, minutes trend, fair price, and the other book's price.

These are statistical leans, not guarantees.

## Bet buttons (FanDuel / DraftKings)

- **Without setup**: each button opens that book's NBA player-prop page for the stat (points, rebounds, assists or threes). From there you find the player.
- **Exact bet slip links (optional)**: add a [The Odds API](https://the-odds-api.com) key as the repository secret `ODDS_API_KEY` (**Settings → Secrets and variables → Actions**). The build then pulls FanDuel and DraftKings player props with `includeLinks=true`, and each button drops that exact over/under onto the bet slip. The Odds API's lines also replace Rotowire's for those two books.
  - Cost: 4 credits per game (one per market). To save credits, the pull is cached and reused for `ODDS_API_REFRESH_MIN` minutes (default 180; set it as a repository variable). A 10-game night at the default is about 160–200 credits, which needs a paid plan. The free 500/month only covers a couple of nights.
  - If the key is missing or the API fails, the board falls back to the prop-page buttons.

## FanDuel bet slip links (free)

FanDuel Research's public GraphQL (the same endpoint used for projected minutes, no key) also returns FanDuel Sportsbook's own `addToBetslip` link for every NBA player points/rebounds/assists/threes line (`fetch_fanduel_props`). Each build pulls it (one request for the whole league; FanDuel caches it 5 minutes) and those lines replace Rotowire's FanDuel lines, so FanDuel buttons put the exact bet on the slip with no setup. It's an undocumented FanDuel endpoint, so it could change; if it fails, FanDuel buttons fall back to the prop pages.

## Parlays

Tap **+ Leg** on any over or under, in a suggested bet or a player's "Bets & game log" view, to add it to a parlay. A bar at the bottom shows the leg count and estimated odds. **Review** opens the parlay:

- Pick **FanDuel** or **DraftKings**. Each leg is priced at that book's line, and a leg the book doesn't offer is flagged.
- Set a stake to see the payout. The model's hit chance multiplies its leg chances, which assumes the legs are independent. It's compared with the chance the book's odds imply.
- It warns about same-game legs (books reprice those), games already started, questionable players, and legs ruled out.
- **With `ODDS_API_KEY` set:** **Confirm & build DraftKings slip** (or FanDuel slip) opens the book with every leg already on the bet slip. It only fills the slip; nothing is placed until you tap Place Bet there. Each leg's own bet slip id comes from The Odds API (`includeSids` / `includeLinks`). Neither book documents a multi-leg link:
  - DraftKings: `sportsbook.draftkings.com/?outcomes=<id>+<id>`. Other builders report this loads a whole parlay.
  - FanDuel: `sportsbook.fanduel.com/addToBetslip?marketId[0]=…&selectionId[0]=…&marketId[1]=…`. Untested.

  If a leg doesn't load (or the book moved that line since the last pull), the sheet keeps a per-leg list. Each leg's Add button uses the book's single-selection link, which is documented.
- **Without the key:** each button opens that stat's player-prop page, and you tap the player's line there.
- **Copy parlay** puts the list on the clipboard.

The parlay is saved in the browser until the slate changes.

## Run locally

```bash
pip install -r requirements.txt
python build.py          # or: ODDS_API_KEY=... python build.py
python -m http.server -d site 8000
```

Then open http://localhost:8000.

## Notes

- Minutes are actual minutes from each player's last 10 (or 5) games, not projections. Rotowire's projected minutes are subscriber-only and aren't used.
- StatMuse is only queried for starters and players listed Ques/Doubt, with a short pause between requests.
- If Rotowire or StatMuse change their page layout, `build.py` will need updating.
