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

## Incident 2026-08-06 to 2026-08-15 — exceloweb.com served static-only for 9 days

**Root cause.** A certbot-generated stub `/etc/nginx/conf.d/exceloweb.com.conf`
was created 2026-08-06. `conf.d/*.conf` loads **alphabetically**, and
`exceloweb.com.conf` sorts before `exceloweb.conf` (`m` < `n`), so the stub won
every `server_name` match and the real config was silently ignored. nginx said so
the whole time:

    nginx: [warn] conflicting server name "exceloweb.com" on 0.0.0.0:443, ignored

**Impact.** The stub had no PHP handler, so the site served static files only:

| Symptom | Detail |
|---|---|
| `/wp-json/` 404 | reported by the `credentials` check as a failing `wp:EW` **credential**. It was never a credential — the check reads 404 as auth failure |
| `/dashboard/` 200 | the CRM `auth_basic` wall was in the ignored file — served **unauthenticated** for 9 days |
| `.php` source disclosure | `xmlrpc.php`, `wp-login.php` served as `application/octet-stream` with readable source. `wp-config.php` was 403, so DB credentials were not exposed |
| `deny-data` bypassed | `/data/*.log` and `.bak` files readable. `leads.log` was 0 bytes, so no lead PII leaked |
| `/voice/hook.php` 404 | voice agent dead; its heartbeat probe reported this as healthy |
| `xmlrpc.php` unblocked | INC-002 looked deployed — the deny rule was in the ignored file |
| autoblog publish failures | `Failed: 404 ... nginx` in `autoblog4sites.log` was the WP REST publish step hitting the dead `wp-json` |

The homepage stayed 200 throughout because `index.html` is static, which is why
nothing looked wrong. Two smoketest checks were reporting the consequences
(`voice_agent`, `credentials`) but attributed them to the wrong causes.

**Fix.** Merged the full real config into `exceloweb.com.conf` (the filename that
wins) and renamed the loser to `exceloweb.conf.RETIRED-20260815`. Backups in
`/home/opc/backups/nginx-20260815/`.

### Failure mode to watch: certbot renewal can recreate the stub

`/etc/letsencrypt/renewal/exceloweb.com.conf` has `installer = nginx`, which is
what created the duplicate server block in the first place. It can happen again
at renewal, on any domain.

**Post-renewal check — run after every certbot run:**

    sudo nginx -t 2>&1 | grep -i conflict

Empty output is good. Any "conflicting server name ... ignored" line means a
vhost is being shadowed: find the duplicate with

    sudo grep -rln "server_name.*<domain>" /etc/nginx/conf.d/*.conf

and remember the **alphabetically first** file wins. Do not assume the file named
after the domain is the live one — verify with `sudo nginx -T`.


## Secrets

Nothing credential-shaped is committed here, and all rollup detail strings are
passed through `mask()` before printing. Env files live in `/etc/*/` and are
excluded by `.gitignore`.
