# Courtside Research

A local, multi-sport live observation and forecast research dashboard. React + TypeScript frontend,
Python standard-library API, and SQLite append-only forecast records.

**Initial implementation, not production-ready.** It does not recommend wagers
or calculate stakes. The separate demo ledger contains synthetic forecasts, ratings, and outcomes.
The new match-outcome ledger uses observed live-source results only.
The logistic model has illustrative coefficients, not trained parameters.
The demo probability range is illustrative, not a confidence interval. Live
experimental probabilities are uncalibrated; no individual confidence interval
or reliable forecasting accuracy is claimed.

## Open on your Mac (no Node.js required)

The prebuilt dashboard is included in `dist/` for easy local use.

1. On this GitHub repository, choose **Code → Download ZIP**.
2. Double-click the downloaded ZIP in Finder to extract `mo-main`.
3. Install Python 3.12 or newer if needed.
4. Open Terminal and run:

```sh
cd ~/Downloads/mo-main
python3 -m server.app
```

Keep Terminal open and visit `http://127.0.0.1:8000` in your browser.
If you extracted the folder elsewhere, use that folder's path instead.
Press Control+C in Terminal to stop the application.

## Run

Requirements: Node 22+ and Python 3.12+. No Python dependencies are required.

```sh
cd /workspace/mo
npm ci --cache /workspace/.npm-cache
npm run build
python -m server.app
```

The API serves the built dashboard on port 8000. It binds to loopback by default.
For frontend development, run `npm run dev` in a second terminal; Vite proxies
`/api` requests to the backend. For a container, set `RESEARCH_HOST=0.0.0.0`.
Never assume running processes survive environment restoration.

```sh
npm test
curl --fail http://127.0.0.1:8000/api/health
```

Data persists in ignored `.data/research.sqlite`; `RESEARCH_DB` overrides this
path. Demo seeding is deterministic and only runs on an empty forecast ledger.
Public observations refresh automatically while the server runs. Demo predictions do not refresh autonomously. Demo event dates are
relative to initial database creation. Do not mistake them for real schedules.

## Live sports research (v0.2)

The **Live research** page collects public observations without API keys:

- ESPN scoreboards and news: Premier League, Champions League, NBA, NHL, NFL,
  and MLB. Scoreboards request a rolling 14-day lookback and 7-day lookahead,
  subject to provider response limits. Each cycle checks today, yesterday,
  tomorrow, and one additional date. Wider coverage fills gradually; it is not
  a complete historical schedule and may take roughly two hours to warm up.
- ESPN structured injury endpoints: NBA, NHL, NFL, only when supported and
  reachable. Unsupported schemas or missing endpoints remain unavailable.
- BBC Sport RSS: general sports news, attributed to the original outlet.

These are public website endpoints, not contractual provider APIs. Availability,
terms, limits, and schemas can change. This app does not scrape article bodies,
access paywalls, or claim permission to redistribute licensed datasets. Review
source terms before public/commercial deployment.

The server checks feeds every five minutes with an 8-second timeout, 12 MB response
size cap, and exponential backoff (up to one hour) on failure. Display refreshes
are every 30 seconds. A manual **Refresh data** reloads local data rather than
forcing extra provider traffic. Stop the app when you no longer need collection.
Set `RESEARCH_LIVE=0` to disable outgoing collection.

Changed source observations are append-only and hashed. Identical content does
not create a duplicate revision, but the successful observation time updates.
A failed feed retains its previous data and marks it unavailable. A record is
stale when not observed successfully within 15 minutes, even if its feed returns
other records. Database files stay local in `.data/`; no data is sent back out.

**Evidence limits:** source injuries are reported statuses, not medical
confirmations. News that mentions an injury is not converted to a structured
injury record. Unknown report times stay unknown. The schedule interval is hours
between the next event's start and the prior known completed event's start;
it does not measure actual rest or physiological fatigue. Travel distance,
workload, complete injury coverage, and an independently validated live forecast model are not
available. An experimental, results-only Elo baseline is available separately. Team season records appear only where the scoreboard supplies them.
Source-supplied scoreboard team statistics and player leader values appear
in event/result details where available; missing statistics stay missing.
Real scores never settle synthetic demo forecasts.

Cloud verification: initial requests were denied by the platform network proxy.
After the network state changed, all 16 configured feeds completed successful
requests: six scoreboards, six ESPN news feeds, three structured injury feeds,
and BBC RSS. ESPN date-range scoreboard queries returned 400; the adapter uses
verified single-day queries instead. The NFL injury response required a larger
capped response size. Tests use clearly identified fixtures. These successful
requests prove current connectivity, not guaranteed future coverage.
The domains `site.api.espn.com` and `feeds.bbci.co.uk` are needed for collection;
on a Mac, allow outgoing HTTPS in your firewall if necessary. **Feed health**
shows actual successes and failures. The app never substitutes demo data for a
failed live feed.

## Match predictions and win/loss tracking (v0.3)

The app opens on **Match predictions**. An experimental Elo model estimates
home/away outcomes for NBA, NHL and MLB; NFL and soccer include a draw class.
It uses observed completed ESPN results only. All forecasts are prospective:
no old games are backfilled into the official forecast ledger or counted as
historical wins. A fresh installation starts with zero scored forecasts.

Eligibility requires a recently checked, scheduled fixture at least 15 minutes
before its reported start, at least 8 observed completed league games, and at
least 2 observed games for both teams. Balanced estimates remain unforecast.
**Not forecast** lists missing-data and timing reasons. Limits are not lowered
because the system has been inactive. Source coverage fills gradually; a new
installation may need time before any event has enough history.

Elo starts at 1500, uses K=20 and scale=400, with no home-advantage coefficient.
These conventional parameters are explicitly experimental, not tuned or claimed
validated. Draw probability uses the observed league draw frequency with a
Dirichlet(1,1,1) prior. The predicted class is the largest estimated probability.
Ratings update for new forecasts as new completed results are observed; released
forecasts never change. Preseason/season records may be mixed by the public
feed, and sparse/incomplete history is a substantial limitation.

### What a win or loss means

- **WIN:** the recorded predicted class matches the reported final-score class.
- **LOSS:** the recorded predicted class does not match it.
- **Pending:** no fresh, scorable final result has been observed yet.

A soccer draw is its own class: predicting a team win on a drawn match is a
loss; correctly predicting a draw is a win. Non-draw sports require a decisive
final score. The target includes overtime and decisive NHL shootout scores in the reported
final score. Standard soccer FT/full-time results are supported. Ambiguous
football penalty statuses, cancellation, missing scores, or stale result feeds
remain ungraded; pending forecasts are still permanently visible. No betting
odds, stakes, financial wins/losses or returns are computed.

Original predictions, training-snapshot references, context, model version,
probabilities, and publication timestamp are hashed and append-only. Publication
rechecks current fixture identity, start, state, hash and source freshness inside
a write transaction. Unique event keys prevent duplicate forecasts across job
retries. Source-reported results are hashed and appended separately. Later
provider corrections do not silently rewrite an already scored result; this
version has no correction-resolution UI, so disputed results require a separate
append-only correction workflow before using them in production reporting.

**Evaluation** shows all recorded correct/incorrect outcomes, accuracy, multiclass
Brier score (sum of class errors), log loss and top-class calibration. The 95%
Wilson interval describes observed classification accuracy under independent
trial assumptions, not confidence in an individual forecast. Zero samples show
no accuracy; a short streak is not validation. This version does not auto-promote
new model families, fit injury coefficients, or guarantee improving accuracy. A heuristic monitoring gate pauses new
forecasts after at least 30 scored forecasts if Brier or log loss is worse than
a uniform-class reference. Existing forecasts continue to be scored. This is
a conservative review policy, not proof of statistical significance; releasing
a revised model requires a separate evaluated implementation.

## Additional sports research sources

**Research library** lists the 17 preferred sources across the requested sports,
including FotMob/NBA/NFL/NHL alternatives, plus the original four Reference-site
sources (21 total). A separate background reader checks public homepages and
robots rules, retaining readable headings and static statistic-table cells with
source URL, fetch time and hash. Successful sources are checked every six hours;
failed sources retry after one hour. Forbidden pages, robots disallows, challenge
pages, unavailable dynamic content and paywalls are not bypassed. A readable
homepage does not establish permission or technical support for bulk/API access.

All readable evidence is used in the research library. Exact team-name matches
available before publication can also be referenced in a forecast's frozen
research context. These references are **not numerical model inputs**. Different
sports, old tables, unknown time ranges, and ambiguous player identities cannot
be defensibly combined as fitted features without dedicated validated adapters.
The model's **Features used** and **Not used** fields state that boundary.
An old update date is shown as reported rather than represented as live data.
Additional sports beyond the configured six leagues have research sources but
no match forecast adapter yet.

The Mac server must stay running, with internet access, for observation,
forecasting and automatic scoring. Data survives restarts in `.data/`. When
installing an update, stop the old server, preserve this hidden folder (Finder:
Command+Shift+period), and copy it into the new extracted project folder before
starting. Never replace or delete a ledger to reset losses. Running the app on
two machines creates separate local ledgers; it does not synchronize them.

## FotMob fixture imports

FotMob is used only as a user-supplied football fixture import format. Its web
JSON interface is not represented as a licensed or documented public API.
Supply a lawfully obtained matches JSON export; no API keys are needed for
offline imports. This environment's proxy denied the attempted live request.

```sh
python -m server.providers /path/to/matches.json
```

Expected shape (example fixture only):

```json
{"leagues":[{"name":"Premier League","matches":[{"id":123,"home":{"name":"Arsenal"},"away":{"name":"Chelsea"},"status":{"utcTime":"2026-10-10T15:00:00Z","finished":false}}]}]}
```

Imports require IDs, names, and timezone-aware kickoff times. They are
idempotent by provider event ID and do not generate forecasts, settle synthetic
predictions, or merge with demo metrics. Repeated imports preserve the original
fixture rather than silently replacing it. Refresh the dashboard after import.

## Integrity and evaluation

- Forecast publication validates probability and feature/publication/start order.
- Forecasts, results, and audit rows reject UPDATE and DELETE through triggers.
- SHA-256 covers original forecast payloads; audit events form a hash chain.
- Results require known source and after-start timestamp; demo outcomes are
  deterministic synthetic values, not actual sports results.
- Brier score, log loss, binary accuracy, and calibration use all settled demo
  forecasts. Football target is home win versus non-win, including draws.
- The chronological walk-forward benchmark fits a historical home-win prior only
  on results recorded before each test window. It is a synthetic-data exercise,
  not evidence of real-world model validity.
- Empty buckets display no metric. Pending records remain visible.
- Read-only HTTP API uses parameter-free queries, escapes React rendering,
  restricts static file paths, and sends security headers.

Hashes detect modification; they do not provide an externally anchored proof.
The database owner can remove triggers. Production needs independent backups,
role-based access, migrations, TLS termination, access/rate limits, real provider
contracts, durable background jobs, trained per-sport models,
validated real-world out-of-sample performance, and monitoring. These are not implemented
or claimed validated in this version.

## Endpoints

`GET /api/outcomes`, `/api/research`, `/api/live`, `/api/overview`, `/api/forecasts`, `/api/events`, `/api/health`, `/api/audit`.
The live endpoint is separate from demo forecast endpoints. No remote write endpoint.

## Container

Build with `docker build -t courtside .`. Run with a persistent volume:

```sh
docker run --rm -p 8000:8000 -v courtside-data:/app/.data courtside
```

Container build is supplied but requires Docker and was not validated here.

### Score-event forecasts

In Match predictions, open **Score forecasts** for combined final-score totals, signed home-team margins, and both teams scoring in soccer. These are non-wagering research events at fixed thresholds, not imported sportsbook lines or ranked plays. Defaults: total thresholds NBA 220.5, NFL 44.5, NHL 5.5, MLB 8.5, soccer 2.5; home margin thresholds ±5.5, ±6.5, ±1.5, ±1.5, ±1.5 respectively. “No” means at or below the threshold; half-point thresholds avoid pushes. Scores use the same provider final-score scope as winner forecasts, including overtime/NHL shootout scoring, and exclude football penalty outcomes.

The experimental score-frequency baseline requires 20 observed completed league matches and five per team. It pools unique matches involving either team, orients score margins toward the fixture home team, and shrinks binary event frequencies toward a Laplace-smoothed league rate with prior weight 10. Shared matches count once. It does not estimate a calibrated full score distribution or use invented injury/news weights. Its minimum sample is an eligibility rule, not proof of accuracy.

The **60%+ estimated probability** filter includes both successful and unsuccessful forecasts based on their original probability. All eligible targets, including those below 60%, stay permanently recorded; exports include the entire score ledger. Accuracy is calculated separately for the selected target/probability cohort, and remains unknown until prospective results are scored. Multiple targets from one match are correlated; total counts are not independent matches. At least 30 distinct scored matches in a target family are required for the heuristic review gate, which pauses new forecasts if binary Brier or log loss exceeds the uniform reference. Existing records still score. There is no automatic model promotion or guarantee of 60% accuracy.

`/api/score-forecasts` exposes immutable forecasts, result snapshots, hashes, per-type evaluation, the 60% cohort, exclusions and monitoring. Keep the server running to collect and score; a fresh installation can need days or weeks to collect enough team history. When updating the GitHub ZIP on Mac, stop the previous server, preserve the hidden `.data` folder in the new project folder, then run `python3 -m server.app`.
