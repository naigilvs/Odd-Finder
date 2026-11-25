# Odd Finder

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

4. Open http://localhost:8765, choose your sportsbook, pick a sport tab and press **Scan**.

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

## Team logos and player photos

Logos and photos come from [TheSportsDB](https://www.thesportsdb.com), a free, community-run sports database. Each name is looked up once, about one every 2.5 seconds because of the free plan's limits, and saved in `images.json`. The first scan of a new league fills its pictures in over a minute or so; after that they load instantly. Names it can't find show initials instead.

Team logos are trademarks of their teams and leagues. They're fine to show in a personal tool like this, but get permission before using them on a public or paid site.

## Stats second opinion

Next to the market's win chance, each moneyline, spread and total in the NFL, NBA, NHL and MLB also gets a **stats win chance** worked out from past games (`stats.py`):

- **Past results:** final scores for last season and this season come from ESPN's public scoreboard feeds, which are free and need no key. They're saved in `data/`, so only new games are downloaded. The very first start takes a minute or two to fill it.
- **Moneyline:** each team has a strength rating (Elo) that rises with wins and falls with losses, adjusted for opponent strength, margin and home advantage.
- **Spread:** the rating gap gives an expected winning margin, and the typical spread of real results around it gives the chance of covering.
- **Totals:** each team's points scored and allowed give an expected game total.
- **Tuning and testing:** settings are tuned on last season and tested on this season, so the accuracy check is honest. See **How accurate are the stats?** on the page. A league or bet type that tests poorly isn't given a stats number.

Bets get a **Market + stats agree** badge when both opinions say the chance of winning is higher than FanDuel's price implies. The stats don't know about injuries or news, so treat them as a check on the market rather than a replacement. Player props, soccer and college sports aren't covered by the stats yet.

### Soccer

The EPL, La Liga, Serie A, Bundesliga and Ligue 1 get a stats opinion from a **goals model** instead of team ratings, because draws are common: each team has an attack and a defense strength (updated after every match), expected goals = league average × attack × the opponent's defense, and the chance of every score follows from that (with a small draw boost tuned on last season). It covers win/draw/loss, goal spreads and totals, but not quarter-goal lines (−0.25, +0.75).

Tested on this season's matches against simple base rates, results were predicted 3–14% better in every league and over/under 2.5 goals 1–5% better, so all five are used. Samples are still small (76–115 matches per league). The Champions League has too few matches per team to test (22), so it stays on the market opinion. Domestic league results are graded automatically; Champions League results and quarter-goal lines aren't (extra time and split results), so you mark those. Soccer player props stay market-only.

### Player props (NBA, NHL, NFL)

Player props also get a stats opinion where it has proven itself (`props_stats.py`):

- **Expected stat** = expected playing time (minutes in the NBA, ice time in the NHL, one game in the NFL) × the player's rate per unit of playing time (recent games weighted more) × how much of that stat the opponent allows compared with an average team.
- **Chance of going over** comes from how far real results landed from the prediction in past games.
- **Data:** each game's box score from ESPN's free game summaries, saved in `data/` and downloaded once.
- **Honest gate:** tuned on last season, tested on this season at lines a sportsbook would offer (the player's recent average). A stat is only used if it beats both a coin flip and the player's recent hit rate. The test reruns every time the model updates. Current results:

| League | Used | Not used |
|---|---|---|
| NBA | points (+2.7%), rebounds (+2.9%), assists (+2.7%) | 3-pointers |
| NHL | points (+3.7%), shots on goal (+3.2%) | |
| NFL | rushing yards (+3.1%), receiving yards (+2.3%), receptions (+2.5%) | passing yards |

## More bets

The **More bets** switch at the top opens a separate section for alternate lines, team totals, halves/quarters/periods, over/under player props and yes/no props (anytime touchdown, double-double, home run…). Nothing there uses credits until you ask:

1. Pick a league. Its list of upcoming games loads for free.
2. Tick the games (up to 10) and the bet types you want. The Scan button shows the most credits it can use: about 1 per bet type per game.
3. Filter the results by league, game, player or team, bet type and confidence. Click any bet for the full explanation.

Results are saved in your browser until the games start. Alternate lines and team totals also get a stats opinion. Yes/no props where books only offer "Yes" can't have their margin removed exactly, so the bot estimates it (about 10%) and marks them Low confidence.

## Files

- `app.py`: the server: downloads odds, values every bet, and serves the page.
- `stats.py`: the stats second opinion from past results.
- `static/index.html`, `static/app.css`, `static/app.js`: the page's layout, styling and behavior.
- `static/calc.js`: the page's calculations (chance of success, confidence, best line per bet).
- `tests/`: automated tests.

## Tests

Run every test with one command. It needs no internet or API key; the page tests need Node.js.

```bash
./run_tests.sh
```

They check the valuation math (margin removal, value, matching lines across books, yes/no margins), bet labels, the stats model, the page's calculations, and the More bets safety checks.

## Saved scans

Your last scan of each league, and your More bets scans, are saved by the bot in `data/scans.json`. They're still there after you restart the bot or the browser, so you don't have to scan again (and spend credits) to see them. Rescanning a league replaces its saved bets; games that have started are dropped automatically. Scans aren't backed up: they're temporary copies of the odds.

## Menu, My picks and Bot results

The **☰** button at the top left opens the menu: **Find bets** (main lines and more bets), **My picks** and **Bot results**.

### My picks
Click the ☆ next to any bet, or **Save pick** in its details (you can add a stake). My picks has two tabs: **Unsettled** (waiting for a result) and **Settled** (won, lost or push).

- **Automatic results:** moneylines, spreads, totals and team totals (including alternate lines) in the NFL, NBA, NHL and MLB are checked after the game from the same free ESPN scores the stats use. Opening the page or pressing **Check results** looks for new ones and uses no credits.
- **NBA, NHL and NFL player props** (points, rebounds, assists, 3-pointers, shots on goal, yards, receptions) are also checked automatically, from the player's real stats in the box score. If an NBA or NHL player didn't play, the bet is a push (sportsbooks void it). NFL box scores only list players who recorded a stat, so if an NFL player isn't listed you mark that one yourself.
- **Marking results yourself:** other player props, quarter/half/period bets and other sports get Won / Lost / Push buttons once the game has started. **Undo** puts a pick back to unsettled.
- **Summary:** your record, how often your picks won compared with the bot's expected chance, the profit if you'd bet $100 on each, your real profit where you entered a stake, and a breakdown by confidence.

### Bot results
Every moneyline, spread, total and team total the bot shows for the NFL, NBA, NHL and MLB, and every NBA, NHL and NFL player prop it can check, is logged automatically when you scan (once per bet; a rescan updates it to the latest price) and checked after the game. The page shows:

- how often finished bets won compared with what the bot expected
- the profit if you'd bet $100 on every **good-value** bet, on every **market + stats agree** bet, and on every bet for comparison
- **When the bot said…**: whether bets the bot gave 50–60% actually won about 55% of the time, and so on
- results by confidence, league and bet type, and the most recent results
- a **Games from** filter (last 7 days, last 30 days, this season, all time) and **Clear history**
- the stats model's own accuracy report

The history is kept (about 1–3 MB a year) in `data/track.json`; saved picks are in `data/picks.json`. Both stay on your computer and off GitHub, and demo mode uses separate files.

## Backups

Your saved picks and the bot's track record are backed up to **iCloud Drive** automatically, a few minutes after anything changes, in a folder called **Odd Finder backups**. Only the latest copy is kept there, so it stays tiny; the GitHub backup below keeps every older version.

If `data/picks.json` or `data/track.json` is missing when the bot starts (for example, the `data` folder was deleted), it's restored from the latest backup automatically and Terminal says so. Your API key (`config.json`) is never backed up. The menu shows when the last backup happened.

To use a different folder, such as Dropbox or Google Drive, add it to `config.json`:

```
"backup_folder": "~/Dropbox/Odd Finder backups"
```

### GitHub backup (optional)

Picks and results can also be saved to a **private** GitHub repo, which keeps every version. Create an empty private repo and add its link to `config.json`:

```
"github_backup": "https://github.com/your-name/your-private-repo.git"
```

Once an hour (and when the bot starts), if anything changed, the bot commits and pushes `picks.json` and `track.json` there. If those files are missing on start, they're restored from iCloud Drive first, then from GitHub. Never use a public repo for this: anyone could see your bets.

Demo mode doesn't make backups.
