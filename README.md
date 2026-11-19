# reapoch-energy-data

This is a static daily mirror of day-ahead electricity prices for training tracker installations. Energinet asks distributed applications to fetch once and serve their own installations. This repository makes that shared fetch and retains the daily files.

Installations use `https://raw.githubusercontent.com/waramos/reapoch-energy-data/main` as the mirror base. Files are under `v1/prices/<provider>/<zone>/<YYYY-MM-DD>.json`, for example `v1/prices/energinet/DK1/2026-11-19.json`.

The data is from Energinet and is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Prices were converted from EUR/MWh to EUR/kWh and expressed as UTC intervals. Each file retains attribution and records these changes. Energinet does not endorse this application.

The builder fetches today and tomorrow in UTC for `energinet:DK1` and `energinet:DK2` with **one request a run**, whatever the number of zones (a second request thirty seconds after the first was answered "429 Too Many Requests"). GitHub Actions runs it at 14:30 and 18:30 UTC daily and on request; a run makes no request at all when both days of every zone are already complete. Tomorrow's prices appear in the early afternoon, Central European time. A failed request fails the run, a day that is not published yet is skipped, and a file is never replaced by one with fewer intervals.

Run `python -m pytest -q` to test offline, and `python build.py` to fetch into this checkout. The builder uses Python 3.11 or later and only the standard library; the tests require pytest. The optional reader compatibility test runs when `training_tracker` is importable. The update workflow commits changed files under `v1/` to `main`; repository settings must allow Actions to write contents.

To add a source, check its licence and whether it allows redistribution first. Add a provider adapter and offline reply tests to `build.py` and `tests/`, then add its `provider:zone` identifier to `sources.json`. Confirm its units, UTC boundaries, publication times, request limits and reader compatibility before enabling it.

The code is licensed under Apache-2.0; see [LICENSE](LICENSE). Data keeps its provider's licence.
