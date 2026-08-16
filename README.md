# ops-assistant

Single-source-of-truth ops CLI for AD's Construction, 3 Thirsty Goats,
ExceloWeb, Liquor Growth, and shared Oracle infra.

    ops morning     what broke, what's unfinished, who needs follow-up,
                    what Claude can finish unattended
    ops status      per-business item status
    ops verify      read-only checks; writes results back to state
    ops broken      machine-verified failures (rollup) + tracked BROKEN items
    ops fix         SAFE pre-approved categories only; everything else escalates
    ops sales       pipeline buckets
    ops unfinished  everything not COMPLETE/HEALTHY
    ops close       verify + snapshot + closeout report (cron 07:40 daily)
    ops history     snapshots + evidence ledger

## Source of truth

`ops` reads **/home/opc/smoketest/rollup.json** (built hourly at :35 by
`/home/opc/smoketest/rollup.py` from `status/*.json`). Facts come from there —
never from alert emails, log lines, or a previous session's memory.
If a system is not in the rollup it is **UNKNOWN, not healthy**.

If the rollup is missing or older than 3h, every command says so loudly.
A stale rollup means the facts are frozen, not fresh.

## last_verified honesty rule

An item is only stamped `last_verified` if a rollup check actually proved it,
via the item's `"rollup_check"` field. Items without that link are reported as
NOT VERIFIED and keep their old date. (Before 2026-08-14, `ops verify` stamped
today's date on every item unconditionally, which made `last_verified` mean
"ops ran" rather than "this works".)

## Proxy checks — green does not mean working

These checks verify a stand-in, not the real outcome. Each **can report GREEN
while the underlying system is failing**. `ops` prints an explicit warning
whenever one of them is passing.

| Check | Why it can lie |
|---|---|
| `ads_autoblog` | Its quota half (posts-this-month vs 6) stays green for the rest of the month once quota is met, even if publishing is broken. Its index-writability half is a real probe, and is what caught the 2026-08-15 PermissionError. Trust the second half. |

**Fixed 2026-08-15** (kept as a record; do not re-add unless they regress):

| Check | Was | Now |
|---|---|---|
| `margin_report` | mtime of a marker written by the exit status of `mail`, so a crashed report still went green | verifies the report file exists, is fresh, non-empty, and contains the real report sections; cron rebuilt so the marker only lands if the report itself succeeded |
| `voice_agent:heartbeat` | returned `True` on every branch, so it could never fail; reported a dead voice line as healthy for 9 days | live-probes the endpoint before reporting call recency |

`health_check.sh` (delegated to by `ops verify`) covers only the sms/lead-guard
pipeline and disk. Its "ALL GREEN" is **not** an all-clear for the estate.

## State: seed vs live

    state/ops_state.seed.json   committed    canonical item/sales definitions
    state/ops_state.json        gitignored   live state + runtime results

`ops close` rewrites the live file nightly, so it is kept out of git to stop the
working tree from being permanently dirty. On first run `load_state()` bootstraps
the live file from the seed. After that, new seed entries are merged into live
**additively** — anything already present in live is never overwritten, so adding
a seeded item cannot clobber recorded verification results.

`last_verified` is always `null` in the seed: a fresh install must never claim a
check it has not actually run.

To add work items, edit the seed and run any `ops` command — the merge is automatic.

## Secrets

Nothing credential-shaped is committed here, and all rollup detail strings are
passed through `mask()` before printing. Env files live in `/etc/*/` and are
excluded by `.gitignore`.
