# IBKR Capability Spike

Throwaway measurement code. Its only job is to replace the assumptions in the
Astor doc with numbers, before those assumptions get baked into the architecture.

## What each mode decides

| Mode | Question | What it changes if the answer is bad |
|---|---|---|
| `observe` | How does a `keepUpToDate=True` 1-min stream really behave? | Bar-close detection strategy, Notify latency budget |
| `ramp` | How many concurrent streams actually work? | Universe size (the doc assumes 50) |
| `pacing` | How long does a bulk backfill take? | Whether Replay can build a 40-day corpus at all |

## Run it

Requires IB Gateway or TWS running and logged in, with the API enabled and
127.0.0.1 in the trusted IPs.

```bash
uv sync
uv run python -m spike.ib_spike observe --count 50 --minutes 20
uv run python -m spike.ib_spike ramp    --start 10 --step 10 --max-subs 60
uv run python -m spike.ib_spike pacing  --count 50 --days 5
```

Use `--port 4002` for IB Gateway paper, `7496`/`7497` for TWS live/paper.
`--client-id` defaults to 17 to avoid colliding with the premarket scanner.

**Run every mode twice — once during RTH and once outside it.** IBKR's
historical and market-data farms behave differently when the market is closed,
and a spike run at 8pm will happily tell you everything works.

Raw events land in `spike/results/<mode>_<timestamp>.jsonl`; summaries print to
stdout.

## Reading the results

### observe

- **Update cadence** — the doc claims 4–6s. If the median is much higher, or the
  p95 is far above the median, real-time Notify has a bigger latency budget
  problem than the design assumes.

- **Bar-completion latency** — the headline number. It measures how long after a
  1-min bar closes we can actually see that it closed. Since Astor evaluates
  only completed bars, the trader's total alert delay is roughly:

  ```
  (time from the move to the bar close, 0-60s) + (this latency) + (scan + notify)
  ```

  If this comes back at 5s, a U&R alert arrives 5–65s after the reclaim. That is
  probably fine for a swing-oriented setup and probably not fine for a scalp.
  Worth knowing before Notify is built, not after.

- **Forming-bar mutations** — a non-zero count confirms partial bars are visible
  mid-interval. This is exactly the live/replay asymmetry that evaluating on
  completed bars is meant to eliminate, so seeing them here is expected and
  harmless. It is also the proof that a "trades above PDL" tick-based rule would
  *not* have been replayable.

- **Missing bars** — the sleeper finding. IBKR emits no bar for a minute in
  which a ticker did not trade. So "a new bar appeared" is **not** a reliable
  signal that the previous bar closed: for a quiet ticker the next bar might be
  three minutes later, and Astor would evaluate three minutes late. The scanner
  must close bars on a **wall-clock timer** and treat a missing bar as
  "no trades", not as "still waiting". Replay must synthesize the same gaps or
  the two paths will disagree.

### ramp

Prints a table per wave. The number to trust is the **highest fully-clean wave**
— every subscription live, every one producing data during the dwell, zero
errors. Watch for error 162 (historical service), 420 (pacing), and 322 (too
many requests).

Note the ramp itself issues one historical request per subscription, and IBKR
allows roughly 60 in any 10-minute window, so a ramp to 60 sits right at that
limit. If failures appear only in the last wave, re-run starting there to tell a
genuine concurrency ceiling apart from a pacing artifact.

### pacing

Extrapolates the Replay corpus build time. If pulling 50 symbols × 5 days is
slow enough that 40 days becomes impractical, Replay needs a local bar cache
(fetch once, reuse forever) — which is worth building early rather than
discovering later.

## Findings so far

Measured **Sun 2026-08-16, market closed**, IB Gateway live on 4001. Everything
below is off-hours evidence — the `observe` and `ramp` numbers still need an RTH
run, and those are the ones the architecture actually hinges on.

**1. Replay corpus cost is a non-issue — if you chunk.**
49 symbols × 5 days of 1-min RTH bars: **4.6s, zero failures**, concurrency 10,
median 0.78s per request. A 40-day corpus is ~8 chunks ≈ 37s. Replay can rebuild
its dataset on demand; a local bar cache is an optimization, not a requirement.

**2. But do NOT fan out large-duration requests.**
A single sequential `2 M` request returns 16,380 bars (42 trading days) fine.
Ten *concurrent* `2 M` requests **all timed out and returned empty**. Duration
ceilings per request are generous (`1 W`→5d, `2 W`→10d, `1 M`→22d, `2 M`→42d),
but the server will not serve them in parallel. Build the corpus from small
chunks at moderate concurrency, not few-and-huge requests.

**3. `ib_async` fails silently on timeout.** `reqHistoricalDataAsync` applies its
own timeout (default 60s) and returns an **empty list instead of raising**. Any
Astor code path that fetches bars must pass `timeout=` explicitly and treat an
empty result as a failure, or a timed-out ticker looks like a ticker that simply
had no data. This bit the spike itself before it was fixed.

**4. `useRTH=True` yields exactly 390 bars/day**, first stamped 09:30, last
15:59 — confirming bar timestamps are the bar *start*, and matching the doc's
decision to ignore pre/post-market. The earliest evaluable completed bar is
09:31, as the doc states.

**5. Historical bars are inconsistently padded — but not for tickers Astor
cares about.** All 49 real day-trading candidates returned a full 390-bar grid
with no empty minutes. Genuinely thin names diverge: `CLIR` returned 390 bars of
which **368 had zero volume** (padded), while `PBHC` returned only 263 bars
(gapped). So IBKR both pads and gaps depending on the name.

Two consequences worth deciding before the scanner is written:

- **Zero-volume bars carry OHLC.** A padded bar repeats the last price, so a
  naive "close is above PDL" test can fire a trigger on a minute in which
  nothing traded. Astor should skip zero-volume bars, or require volume > 0 as a
  trigger precondition.
- **Padding is a live/replay asymmetry risk.** Replay reads a padded historical
  grid; live streaming may simply not emit a bar for a dead minute. If those
  differ, the two paths disagree on quiet tickers. `observe` during RTH is what
  settles this — until then, prefer a wall-clock bar timer over "next bar
  arrived" so the live path does not depend on the answer.

**Still unknown, needs an RTH run:** live update cadence, bar-completion
latency, whether live streams gap, and the real concurrent-subscription ceiling.

## RTH findings (Mon 2026-08-17, 09:45-10:20 ET)

The four questions the off-hours run above couldn't answer. `observe` ran 50
tickers (the user's watchlist first, then filler to 50) for 20 minutes starting
09:45 ET; `ramp` ran separately afterward, 10 tickers/wave up to the pool's
limit, 60s dwell per wave. Raw events:
`spike/results/observe_20260817_094354.jsonl`,
`spike/results/ramp_20260817_102143.jsonl`.

**6. Update cadence and bar-completion latency both hold, roughly.** Cadence:
median 5.0s, mean 6.1s, p95 10.1s, max 60.2s (n=10,040) — close to the doc's
4-6s claim, with a longer tail. Bar-completion latency — how long after a 1-min
bar closes before Astor could act on it — median 5.8s, p95 6.0s, max 9.9s
(n=1,018). **This is the floor on Notify latency**, before any of Astor's own
processing: a setup that completes at bar-close is not alertable until roughly
6s later, minimum, by IBKR's own delivery.

**7. Zero live gaps, on this sample.** All 50 streams produced updates for the
full 20-minute window, including the thin names (TII, SWMR, ZLAB) that showed
heavy zero-volume padding in the Sunday historical pull (finding 5). That's
some evidence the live/replay asymmetry flagged above is less severe than
feared — but 20 minutes on 3 thin names outside the open is a small sample for
a claim this load-bearing. Prefer the wall-clock-timer design regardless; it
doesn't depend on the answer.

**8. Concurrency ceiling is still unmeasured — but at least 70.** Every wave up
to 70 concurrent `keepUpToDate` streams subscribed cleanly and produced updates
during its dwell: zero subscribe failures, zero IBKR errors. The ramp didn't
fail at 70 -- it ran out of *unique* tickers (the ticker file de-duplicated
92 raw entries to 71 before anything reached IBKR, since several user tickers
overlapped the filler list). So this proves "at least 70," not "exactly 70."
Comfortably past the doc's assumed 50-ticker ceiling and past the user's
current watchlist size either way.

**Bottom line:** nothing here breaks the architecture. The one number worth
keeping in mind going forward is the ~6s Notify latency floor — real, and not
something Astor's own code can improve on.

## Known IBKR quirks this is built around

- `keepUpToDate=True` requires `endDateTime=""`.
- Bar timestamps are the bar's **start**. A bar stamped 09:31 covers 09:31–09:32
  and is final at 09:32. The code relies on this when computing completion latency.
- `useRTH=True` restricts to regular hours, which matches the doc's decision to
  ignore pre/post-market for U&R and O&F. It also determines what PDH/PDL mean —
  keep this flag consistent between Replay and Live or the levels will differ.
- Error codes 2104/2106/2158 and friends are farm-status noise, not failures;
  they are filtered from the error counts.
