#!/usr/bin/env python3
"""
ops — single source of truth CLI for AD's Construction, 3 Thirsty Goats,
ExceloWeb, Liquor Growth, prospects, and shared Oracle infra.

Design rules (per spec):
- Never invent status for a system that hasn't actually been checked.
- CURRENTLY VERIFIED (last_verified today) must never be confused with
  LAST_KNOWN_WORKING (most recent time it passed, possibly stale).
- `ops fix` only touches the pre-approved SAFE_FIX_CATEGORIES. Everything
  else becomes a DILLI_DECISION_REQUIRED item instead of being attempted.
- Every `ops verify` / `ops close` run rewrites ops_state.json itself —
  Dilli never pastes terminal output back into a chat.

Usage:
    ops morning
    ops status
    ops verify
    ops fix
    ops sales
    ops unfinished
    ops broken
    ops close
    ops history [item_id]
"""
import copy
import json
import os
import re
import subprocess
import sys
import shutil
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "state", "ops_state.json")        # live, gitignored
SEED_PATH = os.path.join(BASE, "state", "ops_state.seed.json")    # committed
SNAPSHOT_DIR = os.path.join(BASE, "snapshots")
LOG_DIR = os.path.join(BASE, "logs")

# --------------------------------------------------------------------------
# SMOKETEST ROLLUP — the machine-verified source of truth for "what broke".
# ops never asserts a system is healthy from an alert email, a log line, or
# a previous session's memory. If it isn't in the rollup, it is UNKNOWN.
# --------------------------------------------------------------------------
ROLLUP_PATHS = [
    "/home/opc/smoketest/rollup.json",         # actual location
    "/home/opc/smoketest/status/rollup.json",  # fallback if it ever moves
]
# rollup.py runs hourly at :35. Anything older than this means the rollup
# itself has stopped updating and every fact below is frozen, not fresh.
ROLLUP_MAX_AGE_HOURS = 3

# --------------------------------------------------------------------------
# PROXY CHECKS — checks that verify a stand-in, not the real outcome.
# Each of these CAN REPORT GREEN WHILE THE UNDERLYING SYSTEM IS FAILING.
# Never let a green here be read as "this system works".
# --------------------------------------------------------------------------
PROXY_CHECKS = {
    "margin_report": (
        "PROXY: checks mtime of /home/opc/lgos/last_margin_run.txt only. The cron "
        "is `report.py | mail ... && echo date > marker`, so `&&` binds to mail's "
        "exit status, not the report's. If report.py crashes, mail still sends an "
        "EMPTY email, exits 0, and the marker is written -> GREEN on a failed report."
    ),
    "ads_autoblog": (
        "PROXY: monthly quota (posts this month vs 6). Once the month's quota is "
        "met, this stays GREEN for the rest of the month even if autoblog is "
        "completely broken. It measures contract compliance, not that publishing works."
    ),
    "voice_agent:heartbeat": (
        "PROXY: probe_heartbeat() returns True on EVERY branch by design "
        "(no-calls-yet is treated as normal). It can never fail, so a dead voice "
        "call flow is invisible here. Judge voice health by the other probes only."
    ),
}


def mask(text):
    """Redact anything credential-shaped before it reaches stdout or a log."""
    if not text:
        return text
    s = str(text)
    # key=value / key: value forms ONLY. An explicit separator is required so
    # that ordinary prose ("Token has been revoked") is not mangled into
    # nonsense — an unreadable status line is its own kind of failure.
    s = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:pass(?:word|wd)?|token|secret|api[_-]?key|bearer|"
        r"authorization)[A-Z0-9_]*)(\s*[:=]\s*)(\S+)",
        lambda m: f"{m.group(1)}{m.group(2)}***MASKED***",
        s,
    )
    # long high-entropy blobs (hex/base64-ish), e.g. leaked keys in a detail string
    s = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "***MASKED***", s)
    s = re.sub(r"\b(sk|pk|ghp|gho|ghs|AKIA|xox[baprs])[-_A-Za-z0-9]{16,}\b",
               "***MASKED***", s)
    return s


def load_rollup():
    """Return (rollup_dict, path, error_str). Never raises."""
    for p in ROLLUP_PATHS:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    return json.load(f), p, None
            except Exception as e:
                return None, p, f"unreadable: {e}"
    return None, None, f"not found at any of: {', '.join(ROLLUP_PATHS)}"


def rollup_age_hours(rollup):
    ts = (rollup or {}).get("generated_at")
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)
    except Exception:
        return None


def print_rollup_failures(header=True):
    """Print machine-verified failures. Returns list of failing check names."""
    rollup, path, err = load_rollup()
    if header:
        print("MACHINE-VERIFIED FAILURES (smoketest rollup)")
    if err or rollup is None:
        print(f"  !! ROLLUP UNAVAILABLE — {err}")
        print("  !! Treat every system below as UNKNOWN, not healthy.")
        return []

    age = rollup_age_hours(rollup)
    if age is None:
        print("  !! rollup has no usable generated_at — cannot trust freshness.")
    elif age > ROLLUP_MAX_AGE_HOURS:
        print(f"  !! ROLLUP IS STALE ({age}h old, expected <{ROLLUP_MAX_AGE_HOURS}h).")
        print("  !! rollup.py (cron :35 hourly) may have stopped. These facts are")
        print("  !! FROZEN, not fresh — a system could be broken and still absent here.")
    else:
        print(f"  (rollup {age}h old, {rollup.get('total', '?')} checks)")

    failing = rollup.get("failing", []) or []
    if not failing:
        print("  (no failing checks)")
    for c in failing:
        name = c.get("check", "?")
        stale = " [STALE]" if c.get("stale") else ""
        print(f"  - {name}{stale}: {mask(c.get('detail', ''))}")
        if name in PROXY_CHECKS:
            print(f"      ^ {PROXY_CHECKS[name]}")

    # Passing checks that are only proxies must be called out explicitly —
    # a green proxy is the most dangerous line in any status report.
    passing = rollup.get("passing", []) or []
    proxy_greens = [p for p in passing if p in PROXY_CHECKS]
    # sub-probe level proxies (e.g. voice_agent:heartbeat)
    sub_proxies = []
    for c in rollup.get("all", []) or []:
        for pname in (c.get("probes") or {}):
            key = f"{c.get('check')}:{pname}"
            if key in PROXY_CHECKS and (c.get("probes") or {})[pname].get("ok"):
                sub_proxies.append(key)
    if proxy_greens or sub_proxies:
        print("\n  GREEN-BUT-UNTRUSTWORTHY (proxy checks passing):")
        for p in proxy_greens + sub_proxies:
            print(f"  - {p}: {PROXY_CHECKS[p]}")

    return [c.get("check") for c in failing]

VALID_STATUSES = {
    "COMPLETE", "HEALTHY", "ACTION_NEEDED", "BROKEN",
    "BLOCKED", "BACKLOG", "DILLI_DECISION_REQUIRED",
}

# ---- SAFE, PRE-APPROVED fix categories only. Anything else is refused. ----
SAFE_FIX_CATEGORIES = {
    "regenerate_sitemap",
    "restart_known_service",
    "remove_safe_temp_reports",
    "repair_known_cron_entry",
}

# Known safe-to-restart services (health-checked before AND after restart).
RESTARTABLE_SERVICES = ["nginx", "php-fpm", "smsagent", "jasper", "mariadb"]

# HTTP checks ops verify runs (read-only, GET only). Extend as sites are added.
HTTP_TARGETS = {
    "AD's Construction": ["https://adsconstructionllc.com/"],
    "3 Thirsty Goats": ["https://3thirstygoats.com/"],
    "ExceloWeb": ["https://exceloweb.com/"],
    "Liquor Growth": ["https://liquorgrowth.com/"],
}

RUNBOOK_HEALTH_CHECK = os.path.expanduser("~/runbook/bin/health_check.sh")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    """Load live state, bootstrapping from the seed on first run.

    SEED (committed)  = canonical item/sales definitions.
    LIVE (gitignored) = seed + runtime results (last_verified, status, ...).
    `ops close` rewrites LIVE nightly, so keeping it out of git is what stops
    the working tree from being permanently dirty.
    """
    if not os.path.exists(STATE_PATH):
        if os.path.exists(SEED_PATH):
            shutil.copy2(SEED_PATH, STATE_PATH)
            print(f"[bootstrap] live state created from seed -> {STATE_PATH}")
        else:
            sys.exit(f"FATAL: no live state at {STATE_PATH} and no seed at {SEED_PATH}")
    with open(STATE_PATH) as f:
        state = json.load(f)
    _merge_seed(state)
    return state


def _merge_seed(state):
    """Additive-only merge: new seed entries appear in live state.

    Seeding a new item must never clobber runtime results already recorded
    for existing items, so entries present in LIVE are left completely alone.
    """
    if not os.path.exists(SEED_PATH):
        return
    try:
        with open(SEED_PATH) as f:
            seed = json.load(f)
    except Exception as e:
        print(f"[seed] WARNING: {SEED_PATH} unreadable ({e}) — live state used as-is")
        return

    added = []
    live_ids = {i.get("id") for i in state.setdefault("items", [])}
    for it in seed.get("items", []):
        if it.get("id") not in live_ids:
            state["items"].append(copy.deepcopy(it))
            added.append(it.get("id"))
    live_sales = {s.get("company") for s in state.setdefault("sales", [])}
    for s in seed.get("sales", []):
        if s.get("company") not in live_sales:
            state["sales"].append(copy.deepcopy(s))
            added.append(f"sales:{s.get('company')}")

    if added:
        save_state(state)
        print(f"[seed] {len(added)} new entr(y/ies) merged from seed: "
              f"{', '.join(str(a) for a in added)}")


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def curl_status(url, timeout=10):
    """Read-only GET via curl (avoids the urllib/Cloudflare 403 gotcha)."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return out.stdout.strip()
    except Exception as e:
        return f"ERR:{e}"


def service_active(name):
    try:
        out = subprocess.run(["systemctl", "is-active", name],
                              capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "active"
    except Exception:
        return None


# --------------------------------------------------------------------------
# ops morning
# --------------------------------------------------------------------------
def cmd_morning(state):
    items = state["items"]
    today3 = sorted(
        [i for i in items if i["priority"] == "P1"
         and i["status"] in ("ACTION_NEEDED", "BLOCKED", "BROKEN", "DILLI_DECISION_REQUIRED")],
        key=lambda i: i["priority"],
    )[:3]
    dilli_required = [i for i in items if i["status"] == "DILLI_DECISION_REQUIRED"
                      or i.get("requires_dilli")]
    waiting = [i for i in items if i["status"] == "BLOCKED"]
    unfinished = [i for i in items if i["status"] not in ("COMPLETE", "HEALTHY")]
    # What Claude can finish unattended: open, not blocked, and not gated on Dilli.
    claude_can_do = [i for i in items
                     if i["status"] not in ("COMPLETE", "HEALTHY", "BLOCKED")
                     and not i.get("requires_dilli")
                     and i.get("owner") == "claude"]
    sales_today = [s for s in state["sales"] if s["status"] == "FOLLOW_UP_TODAY"]

    print(f"=== OPS MORNING — {now_iso()} ===\n")

    # 1. WHAT BROKE — from the smoketest rollup, never from memory or email.
    print_rollup_failures()

    # 2. WHAT'S UNFINISHED
    print(f"\nUNFINISHED ({len(unfinished)})")
    _print_items(unfinished or ["(nothing open)"])

    print("\nTODAY TOP 3")
    _print_items(today3 or ["(none flagged P1)"])

    # 3. WHO NEEDS FOLLOW-UP
    print("\nWHO NEEDS FOLLOW-UP")
    if sales_today:
        for s in sales_today:
            print(f"  - {s['company']} ({s['contact']}): {s['response']} "
                  f"— next follow-up {s['next_follow_up']}")
    else:
        print("  (nobody due today)")

    # 4. WHAT CLAUDE CAN FINISH WITHOUT DILLI
    print(f"\nCLAUDE CAN FINISH WITHOUT YOU ({len(claude_can_do)})")
    if claude_can_do:
        for i in claude_can_do:
            print(f"  - [{i['business']}] {i['task']}")
            print(f"      next: {i['next_action'] or 'n/a'}")
    else:
        print("  (nothing actionable unattended)")

    print("\nWAITING ON SOMEONE ELSE")
    _print_items(waiting or ["(none)"])

    print("\nNEEDS DILLI (cannot proceed without you)")
    _print_items(dilli_required or ["(none)"])
    # Sales gated on Dilli belong here too — money and messaging to
    # customers/prospects are never things ops acts on by itself.
    dilli_sales = [s for s in state["sales"] if s.get("requires_dilli")]
    for s in dilli_sales:
        print(f"  - [SALES] {s['company']} — {s.get('response') or s.get('offer')}")


def _print_items(items):
    for i in items:
        if isinstance(i, str):
            print(f"  - {i}")
        else:
            print(f"  - [{i['business']}] {i['task']} — {i['status']} "
                  f"(next: {i['next_action'] or 'n/a'})")


# --------------------------------------------------------------------------
# ops status
# --------------------------------------------------------------------------
def cmd_status(state):
    by_biz = {}
    for i in state["items"]:
        by_biz.setdefault(i["business"], []).append(i)
    print(f"=== OPS STATUS — {now_iso()} ===\n")
    for biz, items in by_biz.items():
        print(f"{biz}:")
        for i in items:
            lv = i["last_verified"] or "NEVER VERIFIED"
            lkw = i["last_known_working"] or "NEVER"
            print(f"  [{i['status']}] {i['task']}  "
                  f"(last_verified={lv}, last_known_working={lkw})")
        print()


# --------------------------------------------------------------------------
# ops verify — safe, read-only checks. Writes results back into state.
# --------------------------------------------------------------------------
def cmd_verify(state):
    print(f"=== OPS VERIFY (read-only) — {now_iso()} ===\n")
    ts = now_iso()

    # 1. HTTP reachability, per business
    for biz, urls in HTTP_TARGETS.items():
        for url in urls:
            code = curl_status(url)
            ok = code == "200"
            print(f"[{'PASS' if ok else 'FAIL'}] {biz} site {url} -> {code}")

    # 2. Shared services
    for svc in RESTARTABLE_SERVICES:
        active = service_active(svc)
        if active is None:
            print(f"[SKIP] service {svc} — systemctl not available in this environment")
        else:
            print(f"[{'PASS' if active else 'FAIL'}] service {svc} active={active}")

    # 3. Delegate to the existing runbook health_check.sh if present — do not
    #    reinvent checks that already exist and are trusted.
    if os.path.exists(RUNBOOK_HEALTH_CHECK):
        print(f"\n--- delegating to {RUNBOOK_HEALTH_CHECK} ---")
        try:
            out = subprocess.run([RUNBOOK_HEALTH_CHECK], capture_output=True,
                                  text=True, timeout=60)
            print(out.stdout)
            if out.returncode != 0:
                print(out.stderr)
            print("  ^ SCOPE: health_check.sh covers the sms/lead-guard pipeline "
                  "and disk only. 'ALL GREEN' here is NOT an all-clear for the "
                  "estate — the rollup below is the authority on what broke.")
        except Exception as e:
            print(f"[FAIL] could not run health_check.sh: {e}")
    else:
        print(f"\n[SKIP] {RUNBOOK_HEALTH_CHECK} not found — deploy the runbook "
              f"scripts too, or point RUNBOOK_HEALTH_CHECK at the real path")

    # 4. Machine-verified facts from the smoketest rollup.
    print()
    failing_names = print_rollup_failures()

    rollup, _, rollup_err = load_rollup()
    by_check = {}
    if rollup:
        for c in rollup.get("all", []) or []:
            by_check[c.get("check")] = c

    # 5. Update state HONESTLY. An item is only stamped last_verified if this
    #    pass actually checked it — i.e. it is linked to a rollup check via
    #    "rollup_check". Everything else keeps its old date and is reported as
    #    NOT VERIFIED. Stamping every item unconditionally (the previous
    #    behaviour) made "last_verified" mean "ops ran", not "this works".
    verified, unverified = [], []
    for i in state["items"]:
        link = i.get("rollup_check")
        if not link:
            unverified.append(i)
            continue
        c = by_check.get(link)
        if c is None:
            i["blocker"] = (f"rollup_check '{link}' not present in rollup — "
                            f"check may have stopped running")
            unverified.append(i)
            continue
        i["last_verified"] = ts
        i["evidence"] = f"smoketest:{link} @ {c.get('checked_at')} — {mask(c.get('detail'))}"[:500]
        if c.get("ok"):
            i["last_known_working"] = ts
            if link in PROXY_CHECKS:
                # A green proxy is NOT proof of health. Never promote to HEALTHY.
                i["blocker"] = f"green via PROXY check only — {PROXY_CHECKS[link]}"[:400]
            elif i["status"] in ("BROKEN", "ACTION_NEEDED"):
                i["status"] = "HEALTHY"
                i["blocker"] = None
        else:
            if not i.get("requires_dilli"):
                i["status"] = "BROKEN"
            i["blocker"] = mask(c.get("detail"))[:400]
        verified.append(i)

    save_state(state)

    print(f"\n--- STATE UPDATE ({ts}) ---")
    print(f"VERIFIED by this pass ({len(verified)}):")
    for i in verified:
        print(f"  - {i['id']} -> {i['status']}")
    print(f"NOT VERIFIED by this pass ({len(unverified)}) — last_verified left "
          f"UNCHANGED, status is UNKNOWN not healthy:")
    for i in unverified:
        print(f"  - {i['id']} (last_verified={i['last_verified'] or 'NEVER'}) "
              f"— add \"rollup_check\" to link it to a smoketest check")
    if rollup_err:
        print(f"\n!! rollup unavailable ({rollup_err}) — nothing could be "
              f"machine-verified this pass.")


# --------------------------------------------------------------------------
# ops fix — SAFE categories only. Never money, messages, pricing, contracts,
# data destruction, credential rotation, or architecture changes.
# --------------------------------------------------------------------------
def cmd_fix(state):
    print(f"=== OPS FIX (safe categories only) — {now_iso()} ===\n")
    acted = []
    for i in state["items"]:
        if i["status"] != "BROKEN":
            continue
        # Only known-safe automatic fix: restart a service proven down by verify.
        # Everything else in this list becomes DILLI_DECISION_REQUIRED —
        # never guessed at.
        i["status"] = "DILLI_DECISION_REQUIRED"
        i["blocker"] = (i["blocker"] or "") + " [ops fix could not safely resolve — escalated]"
        acted.append(i["id"])
    if acted:
        save_state(state)
        print(f"Escalated to DILLI_DECISION_REQUIRED: {', '.join(acted)}")
    else:
        print("Nothing BROKEN in current state, or no safe automated fix applies.")
    print("\nSafe categories this command is allowed to perform:")
    for c in sorted(SAFE_FIX_CATEGORIES):
        print(f"  - {c}")
    print("\nIt will NEVER: spend money, message customers/prospects, change "
          "pricing/contracts, destroy data, rotate credentials, or make "
          "production architecture changes. Those always land as "
          "DILLI_DECISION_REQUIRED.")


# --------------------------------------------------------------------------
# ops sales
# --------------------------------------------------------------------------
def cmd_sales(state):
    print(f"=== OPS SALES — {now_iso()} ===\n")
    buckets = {"FOLLOW_UP_TODAY": [], "READY_TO_SEND": [], "WAITING": [],
               "BLOCKED": [], "STALE": []}
    for s in state["sales"]:
        buckets.setdefault(s["status"], []).append(s)
    known = ["FOLLOW_UP_TODAY", "READY_TO_SEND", "WAITING", "BLOCKED", "STALE"]
    for label, key in [("FOLLOW UP TODAY", "FOLLOW_UP_TODAY"),
                        ("READY TO SEND", "READY_TO_SEND"),
                        ("WAITING", "WAITING"), ("BLOCKED", "BLOCKED"),
                        ("STALE", "STALE")]:
        print(label)
        if buckets[key]:
            for s in buckets[key]:
                _print_sale(s)
        else:
            print("  (none)")
        print()

    # Catch-all: an entry with an unrecognised status used to be silently
    # invisible here — money quietly falling out of the report is exactly the
    # failure mode this tool exists to prevent.
    for key, entries in buckets.items():
        if key not in known and entries:
            print(f"{key} (unrecognised status — fix or add to the known list)")
            for s in entries:
                _print_sale(s)
            print()


def _print_sale(s):
    money = f" [{s['offer']}]" if s.get("offer") else ""
    who = s.get("contact") or "contact NOT RECORDED"
    print(f"  - {s['company']} ({who}){money}")
    if s.get("response"):
        print(f"      {s['response']}")
    if s.get("next_follow_up"):
        print(f"      next follow-up: {s['next_follow_up']}"
              f"{'  [NEEDS DILLI]' if s.get('requires_dilli') else ''}")


# --------------------------------------------------------------------------
# ops unfinished / ops broken
# --------------------------------------------------------------------------
def cmd_unfinished(state):
    items = [i for i in state["items"] if i["status"] not in ("COMPLETE", "HEALTHY")]
    print(f"=== OPS UNFINISHED — {now_iso()} ({len(items)}) ===\n")
    _print_items(items or ["(nothing open)"])


def cmd_broken(state):
    """What broke — machine-verified facts first, tracked items second."""
    print(f"=== OPS BROKEN — {now_iso()} ===\n")
    print_rollup_failures()

    items = [i for i in state["items"] if i["status"] == "BROKEN"]
    print(f"\nTRACKED ITEMS MARKED BROKEN ({len(items)})")
    _print_items(items or ["(none)"])


# --------------------------------------------------------------------------
# ops close — verify, snapshot, and print the closeout report.
# --------------------------------------------------------------------------
def cmd_close(state):
    print("Running verify before close...\n")
    cmd_verify(state)
    state = load_state()

    completed = [i for i in state["items"] if i["status"] == "COMPLETE"]
    healthy = [i for i in state["items"] if i["status"] == "HEALTHY"]
    broken = [i for i in state["items"] if i["status"] == "BROKEN"]
    blocked = [i for i in state["items"] if i["status"] == "BLOCKED"]
    dilli_req = [i for i in state["items"] if i["status"] == "DILLI_DECISION_REQUIRED"]
    p1_open = [i for i in state["items"]
               if i["priority"] == "P1" and i["status"] not in ("COMPLETE", "HEALTHY")][:3]

    print(f"\n=== OPS CLOSE — {now_iso()} ===\n")
    print("COMPLETED"); _print_items(completed or ["(none)"])
    print("\nHEALTHY"); _print_items(healthy or ["(none)"])
    print("\nBROKEN"); _print_items(broken or ["(none)"])
    print("\nBLOCKED"); _print_items(blocked or ["(none)"])
    print("\nREADY FOR DILLI"); _print_items(dilli_req or ["(none)"])
    print("\nTOMORROW TOP 3"); _print_items(p1_open or ["(none flagged P1)"])

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snap_path = os.path.join(SNAPSHOT_DIR, f"{now_iso()}.json")
    with open(snap_path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nSnapshot written: {snap_path}")


# --------------------------------------------------------------------------
# ops history
# --------------------------------------------------------------------------
def cmd_history(state, item_id=None):
    if item_id:
        entries = [e for e in state["evidence_ledger"] if e["gate_id"] == item_id]
        print(f"=== HISTORY for {item_id} ===\n")
        if not entries:
            print("(no evidence ledger entries — nothing has been closed under this id yet)")
        for e in entries:
            print(json.dumps(e, indent=2))
    else:
        print("=== SNAPSHOTS ===")
        if os.path.isdir(SNAPSHOT_DIR):
            for f in sorted(os.listdir(SNAPSHOT_DIR)):
                print(f"  - {f}")
        else:
            print("  (no snapshots yet — run `ops close`)")
        print("\n=== EVIDENCE LEDGER ===")
        for e in state["evidence_ledger"]:
            print(f"  - {e['gate_id']}: {e['status']} (verified {e['date']}, commit {e['commit']})")


COMMANDS = {
    "morning": cmd_morning,
    "status": cmd_status,
    "verify": cmd_verify,
    "fix": cmd_fix,
    "sales": cmd_sales,
    "unfinished": cmd_unfinished,
    "broken": cmd_broken,
    "close": cmd_close,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in list(COMMANDS) + ["history"]:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    state = load_state()
    if cmd == "history":
        cmd_history(state, sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        COMMANDS[cmd](state)


if __name__ == "__main__":
    main()
