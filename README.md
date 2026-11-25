# Value Finder

Scans every bet a sportsbook (FanDuel for now) offers, estimates each one's real chance of winning, and ranks them by **expected value**: how much the odds pay relative to that chance.

## How it works

1. It pulls FanDuel's current odds, plus the odds for the same games from about 9 other books (Pinnacle, DraftKings, BetMGM, Caesars and others), from [The Odds API](https://the-odds-api.com).
2. For every FanDuel line it finds the same line at the other books, removes each book's built-in margin (the vig), and averages the results into an **estimated win probability**. Low-margin books like Pinnacle count more.
3. **Expected value = estimated win % × decimal odds − 1.** Bets are listed with the highest EV at the top.

Bet types: moneyline, point spread, totals (over/under) and, if you turn them on, player props (NBA, NFL, MLB, NHL, college football and basketball). Games that have already started are skipped.

## Setup

1. Get a free API key at https://the-odds-api.com. The free plan includes 500 credits a month.
2. Copy `config.example.json` to `config.json` and paste your key into it, or set the `ODDS_API_KEY` environment variable.
3. Start the app:

   ```bash
   python3 app.py
   ```

4. Open http://localhost:8765, pick the sports and press **Scan odds**.

To try it without a key, run `python3 app.py --demo`. It uses made-up teams and odds.

## API credit costs

- Each sport scanned costs 3 credits (moneyline, spread and totals). Results are cached for 5 minutes, so repeat scans in that window are free.
- Player props cost up to about 4 credits per game, for at most 8 games per sport per scan.
- The number of credits left is shown next to the Scan button.

## Adding another sportsbook later

Add it to `TARGET_BOOKS` in `app.py`, using The Odds API's bookmaker key, for example `"draftkings": "DraftKings"`.

## Limits

- The estimate comes from the betting market. It's not a guarantee. A positive EV only pays off on average over many bets.
- Odds move fast. Check the line in the FanDuel app before you bet.
- Futures and outrights (for example "who wins the championship") are not included.
