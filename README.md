# Courtside Research

A local, multi-sport live observation and forecast research dashboard. React + TypeScript frontend,
Python standard-library API, and SQLite append-only forecast records.

**Initial implementation, not production-ready.** It does not recommend wagers
or calculate stakes. All bundled forecasts, ratings, and outcomes are synthetic.
The logistic model has illustrative coefficients, not trained parameters.
The displayed probability range is illustrative, not a confidence interval.

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

The default **Live research** page collects public observations without API keys:

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
workload, complete injury coverage, and a validated live forecast model are not
available. Team season records appear only where the scoreboard supplies them.
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

`GET /api/live`, `/api/overview`, `/api/forecasts`, `/api/events`, `/api/health`, `/api/audit`.
The live endpoint is separate from demo forecast endpoints. No remote write endpoint.

## Container

Build with `docker build -t courtside .`. Run with a persistent volume:

```sh
docker run --rm -p 8000:8000 -v courtside-data:/app/.data courtside
```

Container build is supplied but requires Docker and was not validated here.
