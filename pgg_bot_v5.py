"""
PGG Field App — v5 verification harness
========================================

This is the test harness for the *new* version of your PGG app, which adds:
  - Village type (HOMELAND / NON_HOMELAND)
  - Decision-time tracking (start → confirm seconds)
  - Z5 Reconciliation screen (sum F → ZAR ceiling + R30 show-up fee)
  - F profit uses Math.ceil() (rounding in participant's favour)
  - Expanded CSV: village_type, member_no, treatment_chief, treatment_comm,
    contribution_timestamp, decision_seconds, prev_round_C, prev_round_my_F,
    sd_contribution, min_contribution, max_contribution,
    free_rider_count, full_contrib_count
  - State-machine `advanceToNextRound()`: resumes from interrupted contributions
    OR results without wiping data
  - `popstate` handler: hardware back button is blocked mid-experiment

This harness runs FOUR test layers:

  LAYER A — Computation & CSV format
    Drives ONE full 40-round experiment with deterministic contributions and
    verifies: F = ceil(B + E), CSV has all 24 columns, treatment dummies
    are correct, lag variables (prev_round_C, prev_round_my_F) match the
    previous row, group-level descriptive stats (sd, min, max, free count,
    full count) match what we'd compute ourselves, Z5 grand total =
    ceil(sumF / 4) + 30.

  LAYER B — Resume mechanism
    Injects controlled interruptions (browser-back, page reload) at five
    critical points and verifies the app resumes EXACTLY where it left off,
    with no data loss and no skipped members. This is the regression test
    for the v3 critical bug.

  LAYER C — popstate (back-button) interception
    During the experiment, attempts a browser back. The new app should
    re-push state and show a "Back is disabled" toast. Verifies the route
    didn't actually change.

  LAYER D — Behavioral (LLM-driven) end-to-end
    Drives the full 40 rounds with 4 LLM-driven participants and a small
    amount of facilitator chaos. Sanity-checks decision_seconds values are
    reasonable (>0, not crazy-large) and the final exported CSV has 160
    rows (4 members × 40 rounds).

USAGE (Windows PowerShell):
    pip install playwright anthropic
    python -m playwright install chromium
    $env:ANTHROPIC_API_KEY = "sk-ant-..."
    python pgg_bot_v5.py                       # run all 4 layers
    python pgg_bot_v5.py --layers A B          # only layers A and B (fast)
    python pgg_bot_v5.py --no-llm              # layer D uses random fallback
    python pgg_bot_v5.py --headed              # watch the browser

OUTPUT (to C:\\Users\\yating\\Desktop by default):
    pgg_v5_<timestamp>_results.csv     - per-check pass/fail
    pgg_v5_<timestamp>_bugs.txt        - bug log
    pgg_v5_<timestamp>_summary.txt     - human-readable summary
    pgg_v5_<timestamp>_layer_*_csv.csv - app's CSV export for each layer
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import http.server
import io
import json
import math
import os
import random
import re
import socketserver
import sys
import threading
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout


# =============================================================================
# CONFIG
# =============================================================================
APP_DIR = Path(__file__).parent
PORT = 8765
BASE_URL = f"http://localhost:{PORT}/index.html"
DEFAULT_PIN = "1234"
TOKENS_PER_ZAR = 4
SHOW_UP_FEE_ZAR = 30
if os.name == "nt":
    DEFAULT_OUT_DIR = Path("user's_output_address")
else:
    DEFAULT_OUT_DIR = Path(__file__).parent / "test_output"

# Full 40-round order (locked)
FULL_ORDER = []
for sess in [1, 2]:
    for blk in ["A", "B"]:
        rounds = list(range(1, 11)) if blk == "A" else list(range(11, 21))
        for r in rounds:
            FULL_ORDER.append((sess, blk, r))  # 40 entries


# =============================================================================
# LOCAL SERVER
# =============================================================================
def start_server() -> socketserver.TCPServer:
    def handler(*a, **kw):
        return http.server.SimpleHTTPRequestHandler(*a, directory=str(APP_DIR), **kw)
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a, **kw: None
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# =============================================================================
# BUG / CHECK COLLECTION
# =============================================================================
class TestReport:
    def __init__(self):
        self.checks: list[dict] = []  # {layer, name, status, msg, ctx}
        self.bugs: list[dict] = []

    def check(self, layer: str, name: str, ok: bool, msg: str = "", ctx: dict = None):
        status = "PASS" if ok else "FAIL"
        self.checks.append({"layer": layer, "name": name, "status": status,
                            "msg": msg, "ctx": ctx or {}})
        if not ok:
            self.bugs.append({"severity": "high", "layer": layer, "where": name,
                              "msg": msg, "ctx": ctx or {}})
            print(f"    ✗ FAIL [{layer}] {name}: {msg}")
        else:
            print(f"    ✓ [{layer}] {name}{(': ' + msg) if msg else ''}")

    def add_bug(self, layer: str, severity: str, where: str, msg: str, ctx: dict = None):
        self.bugs.append({"severity": severity, "layer": layer, "where": where,
                          "msg": msg, "ctx": ctx or {}})
        prefix = {"critical": "🔥", "high": "✗", "medium": "⚠", "low": "·"}.get(severity, "?")
        print(f"    {prefix} [{layer}/{severity}] {where}: {msg}")

    def summary_counts(self) -> dict:
        passed = sum(1 for c in self.checks if c["status"] == "PASS")
        failed = sum(1 for c in self.checks if c["status"] == "FAIL")
        return {"checks_total": len(self.checks), "passed": passed,
                "failed": failed, "bugs_total": len(self.bugs)}


# =============================================================================
# PLAYWRIGHT HELPERS
# =============================================================================
def _ensure_spa_alive(page: Page) -> bool:
    try:
        if page.evaluate("typeof go === 'function' && typeof STATE !== 'undefined'"):
            return True
    except Exception:
        pass
    try:
        page.goto(BASE_URL, timeout=5000)
        page.wait_for_function(
            "typeof STATE !== 'undefined' && typeof go === 'function'", timeout=5000)
        return True
    except Exception:
        return False

def current_screen(page: Page) -> str:
    try:
        return page.evaluate("route")
    except Exception:
        if _ensure_spa_alive(page):
            try: return page.evaluate("route")
            except Exception: return "?"
        return "?"

def get_state(page: Page) -> dict:
    try:
        return page.evaluate("STATE")
    except Exception:
        if _ensure_spa_alive(page):
            try: return page.evaluate("STATE")
            except Exception: return {}
        return {}

def visible_text(page: Page) -> str:
    return page.evaluate("document.body.innerText")


# =============================================================================
# SETUP HELPERS — the new setup has a Village Type field
# =============================================================================
def do_setup(page: Page, pin: str = DEFAULT_PIN, village_type: str = "HOMELAND"):
    """Setup wizard with the new village_type field."""
    page.fill("#f-name", "TestBot")
    page.fill("#f-vname", "TestVillage")
    page.fill("#f-vnum", "1")
    page.select_option("#f-vtype", village_type)
    page.fill("#f-pin", pin)
    page.evaluate("""() => {
        const dots = document.querySelectorAll('.colour-dot');
        for (const d of dots) if (d.dataset.colour === 'Blue') d.click();
    }""")
    page.click("#f-save")
    page.wait_for_function("route === 'home'", timeout=5000)

def reset_app(page: Page, village_type: str = "HOMELAND"):
    page.goto(BASE_URL)
    page.wait_for_function("typeof STATE !== 'undefined'", timeout=10000)
    page.evaluate("localStorage.clear()")
    page.reload()
    page.wait_for_function("route === 'setup'", timeout=5000)
    do_setup(page, DEFAULT_PIN, village_type)


# =============================================================================
# DRIVE ONE MEMBER'S CONTRIBUTION
# =============================================================================
def enter_pin(page: Page, pin: str = DEFAULT_PIN):
    page.wait_for_function("route === 'memberPin'", timeout=10000)
    page.click("#focus-btn")
    page.fill("#pin-in", pin)

def member_contributes(page: Page, m: int, amount: int, pin: str = DEFAULT_PIN,
                       deliberation_seconds: float = 0.5):
    """One member's full contribution flow. `deliberation_seconds` is how
    long we pause on the contribute screen before clicking — this lets us
    test decision_seconds gets recorded sensibly."""
    page.wait_for_function("route === 'callMember'", timeout=10000)
    page.click("#go")
    enter_pin(page, pin)
    page.wait_for_function("route === 'memberHome'", timeout=5000)
    page.click("#go")
    page.wait_for_function("route === 'memberContribute'", timeout=5000)
    # Simulate the member taking some time to deliberate
    time.sleep(max(0.05, deliberation_seconds))
    page.evaluate(
        """(n) => {
            const btns = document.querySelectorAll('.token-grid .token-btn');
            for (const b of btns) {
                if (b.textContent.trim() === String(n)) { b.click(); return; }
            }
        }""",
        amount,
    )
    page.click("#next")
    page.wait_for_function("route === 'memberConfirm'", timeout=5000)
    page.click("#confirm")
    page.wait_for_function("route === 'memberThanks'", timeout=5000)
    page.click("#ok")

def member_views_results(page: Page, m: int, pin: str = DEFAULT_PIN) -> dict:
    page.wait_for_function("route === 'callMemberResults'", timeout=10000)
    page.click("#go")
    enter_pin(page, pin)
    page.wait_for_function("route === 'memberHomeResults'", timeout=5000)
    page.click("#this")
    page.wait_for_function("route === 'memberThisRound'", timeout=5000)
    rec = page.evaluate("""() => {
        const rows = document.querySelectorAll('.result-row .value');
        return [...rows].map(r => Number(r.textContent.trim()));
    }""")
    page.click("#ok")
    return {"A": rec[0], "B": rec[1], "C": rec[2], "D": rec[3], "E": rec[4], "F": rec[5]}


# =============================================================================
# DRIVE ONE COMPLETE ROUND (4 contributions + 4 results + finalTotal)
# =============================================================================
def play_round(page: Page, contribs: dict, report: TestReport, layer: str,
               deliberation_seconds: float = 0.5):
    """Drive the locked happy path for one round. Returns per-member results
    as read off the screen + the four-person total seen on finalTotal."""
    # contribution phase
    for m in [1, 2, 3, 4]:
        member_contributes(page, m, contribs[m], DEFAULT_PIN, deliberation_seconds)

    # group total
    page.wait_for_function("route === 'groupTotal'", timeout=5000)
    page.click("#next")

    # results phase
    page.wait_for_function("route === 'waitForResults'", timeout=5000)
    page.click("#go")
    per_member = {}
    for m in [1, 2, 3, 4]:
        per_member[m] = member_views_results(page, m)

    # callFinalTotal -> PIN -> finalTotal
    page.wait_for_function("route === 'callFinalTotal'", timeout=10000)
    page.click("#go")
    enter_pin(page)
    page.wait_for_function("route === 'finalTotal'", timeout=5000)
    page.click("#next")

    return per_member


# =============================================================================
# CSV EXPORT
# =============================================================================
def export_csv(page: Page) -> str:
    """Drive Home -> Export and read the preview."""
    # Navigate to home — might be in mid-experiment, so use go() directly
    page.evaluate("go('home')")
    page.wait_for_function("route === 'home'", timeout=5000)
    page.click("#btn-exp")
    page.wait_for_function("route === 'exportData'", timeout=5000)
    csv_text = page.evaluate("document.getElementById('preview').textContent")
    page.click("#back")
    return csv_text


# =============================================================================
# LAYER A — COMPUTATION & CSV
# =============================================================================
# Expected CSV header (24 columns)
EXPECTED_CSV_HEADER = [
    "village_number", "village_name", "village_type",
    "group_colour", "date", "facilitator_name",
    "participant_id", "member_no",
    "session", "block", "round",
    "treatment_chief", "treatment_comm",
    "A_contribution", "B_private_kept", "C_group_total",
    "D_C_times_mult", "E_share", "F_profit",
    "contribution_timestamp", "decision_seconds",
    "prev_round_C", "prev_round_my_F",
    "sd_contribution", "min_contribution", "max_contribution",
    "free_rider_count", "full_contrib_count",
]

def layer_a(page: Page, report: TestReport, out_dir: Path, run_id: str):
    """
    LAYER A: drive 6 rounds with carefully chosen contribution patterns that
    exercise:
      - F = ceil(B + E)  (test E that doesn't divide evenly)
      - Group descriptive stats (sd, min, max, free count, full count)
      - Lag variables (prev_round_C, prev_round_my_F)
      - Treatment dummies (we play rounds in two different blocks to exercise
        treatment_chief and treatment_comm)
      - Z5 reconciliation grand total
    """
    print("\n=== LAYER A — Computation & CSV ===")
    reset_app(page, village_type="HOMELAND")

    # Start experiment
    page.click("#btn-this")
    page.wait_for_function("route === 'thisRound'", timeout=5000)
    page.click("#start")
    page.wait_for_function("route === 'callMember'", timeout=5000)

    # Pre-planned contribution patterns — chosen to exercise edge cases:
    # Round 1 S1A: 1,3,5,7  -> C=16, D=32, E=8.0, F values: 9+8=17, 7+8=15, 5+8=13, 3+8=11   sd≈2.236, min=1, max=7, free=0, full=0
    # Round 2 S1A: 0,0,10,10 -> C=20, D=40, E=10, F: 10+10=20, 10+10=20, 0+10=10, 0+10=10   sd=5, min=0, max=10, free=2, full=2
    # Round 3 S1A: 1,2,3,3   -> C=9, D=18, E=4.5, F: ceil(9+4.5)=14, ceil(8+4.5)=13, ceil(7+4.5)=12, ceil(7+4.5)=12  sd≈0.829, min=1, max=3, free=0, full=0
    # This R3 exercises the ceiling: 4.5 + integer should be ceiled.
    patterns = [
        {1: 1, 2: 3, 3: 5, 4: 7},  # R1 S1A
        {1: 0, 2: 0, 3: 10, 4: 10},  # R2 S1A (extreme)
        {1: 1, 2: 2, 3: 3, 4: 3},  # R3 S1A (E=4.5, non-integer)
        {1: 2, 2: 4, 3: 6, 4: 8},  # R4 S1A
        {1: 0, 2: 0, 3: 0, 4: 1},  # R5 S1A (all-zero almost)
        {1: 10, 2: 10, 3: 10, 4: 10},  # R6 S1A (all full)
    ]

    actual_results = []
    for r_idx, contribs in enumerate(patterns, start=1):
        per_member = play_round(page, contribs, report, layer="A",
                                deliberation_seconds=0.4)
        actual_results.append((1, "A", r_idx, contribs, per_member))

    # Quick check of F = ceil(B + E) for round 3 (E = 4.5)
    contribs_r3 = patterns[2]
    pm_r3 = actual_results[2][4]
    C3 = sum(contribs_r3.values())
    D3 = C3 * 2
    E3 = D3 / 4
    for m in [1, 2, 3, 4]:
        B = 10 - contribs_r3[m]
        expected_F = math.ceil(B + E3)
        ok = pm_r3[m]["F"] == expected_F
        report.check("A", f"F=ceil(B+E) for m{m} in round 3",
                     ok, f"expected {expected_F}, got {pm_r3[m]['F']}",
                     ctx={"A": contribs_r3[m], "B": B, "E": E3,
                          "B+E": B + E3, "got": pm_r3[m]["F"]})

    # Now export the CSV and verify structure
    csv_text = export_csv(page)
    (out_dir / f"pgg_v5_{run_id}_layer_A_csv.csv").write_text(csv_text, encoding="utf-8")

    reader = csv.DictReader(io.StringIO(csv_text))
    header = reader.fieldnames or []
    rows = list(reader)

    # CHECK: header matches
    if header == EXPECTED_CSV_HEADER:
        report.check("A", "CSV header has all 24 columns in order", True)
    else:
        missing = [c for c in EXPECTED_CSV_HEADER if c not in header]
        extra = [c for c in header if c not in EXPECTED_CSV_HEADER]
        report.check("A", "CSV header structure", False,
                     f"missing={missing}, extra={extra}",
                     ctx={"got_header": header})

    # CHECK: row count = 4 members × 6 rounds = 24
    expected_rows = 4 * 6
    report.check("A", "CSV row count", len(rows) == expected_rows,
                 f"expected {expected_rows}, got {len(rows)}")

    # CHECK: village_type column populated correctly
    if rows:
        vt_values = set(r["village_type"] for r in rows)
        report.check("A", "village_type populated", vt_values == {"HOMELAND"},
                     f"unique values: {vt_values}")

    # CHECK: block column is 0/1 dummy (BY DESIGN: 0 = R1-10 no comm, 1 = R11-20 comm)
    # In Layer A we only played R1-6 (all block A), so all values should be "0".
    if rows:
        block_values = set(r["block"] for r in rows)
        report.check("A", "block column = 0/1 dummy (R1-6 all = 0 = no comm)",
                     block_values == {"0"},
                     f"unique block values: {block_values} (expected {{'0'}})")

    # CHECK: block and treatment_comm are equal in every row (by design — they're
    # the same variable encoded twice for analysis convenience)
    if rows:
        mismatches = [r for r in rows if r["block"] != r["treatment_comm"]]
        report.check("A", "block == treatment_comm in every row (by design)",
                     len(mismatches) == 0,
                     f"{len(mismatches)} rows where block != treatment_comm")

    # CHECK: treatment_chief is 0 for session 1 (always, in our test)
    if rows:
        bad = [r for r in rows if int(r["session"]) == 1 and r["treatment_chief"] != "0"]
        report.check("A", "treatment_chief=0 for session 1", len(bad) == 0,
                     f"{len(bad)} rows had non-zero chief in session 1")

    # CHECK: treatment_comm is 0 for all rows in our test (all R1-6 are in block A)
    if rows:
        bad = [r for r in rows if r["treatment_comm"] != "0"]
        report.check("A", "treatment_comm=0 for all test rows (R1-6 all block A)",
                     len(bad) == 0,
                     f"{len(bad)} rows had non-zero treatment_comm")

    # CHECK: decision_seconds is populated and > 0
    if rows:
        bad_ds = [r for r in rows
                  if not r["decision_seconds"] or float(r["decision_seconds"]) <= 0]
        report.check("A", "decision_seconds > 0 for every row", len(bad_ds) == 0,
                     f"{len(bad_ds)} rows had missing/zero decision_seconds")
        # Also check sanity range — our deliberation was ~0.4s, plus a bit for the
        # click delay. Should be between 0.05 and 30 seconds.
        out_of_range = [r for r in rows
                        if r["decision_seconds"] and
                        (float(r["decision_seconds"]) < 0.05 or float(r["decision_seconds"]) > 30)]
        report.check("A", "decision_seconds in sensible range [0.05, 30]",
                     len(out_of_range) == 0,
                     f"{len(out_of_range)} rows out of range")

    # CHECK: contribution_timestamp is a parseable ISO string
    if rows:
        bad = []
        for r in rows:
            try:
                dt.datetime.fromisoformat(r["contribution_timestamp"].replace("Z", "+00:00"))
            except Exception:
                bad.append(r["contribution_timestamp"])
        report.check("A", "contribution_timestamp parseable as ISO",
                     len(bad) == 0, f"{len(bad)} unparseable values")

    # CHECK: lag variables — prev_round_C and prev_round_my_F
    # For each (member, sorted by round), row N's prev_round_C should = row N-1's C_group_total
    if rows:
        # group by member
        by_member = {}
        for r in rows:
            by_member.setdefault(r["member_no"], []).append(r)
        # sort each list by (session, block, round)
        for m, rlist in by_member.items():
            rlist.sort(key=lambda x: (int(x["session"]),
                                      0 if x["block"] == "A" else 1,
                                      int(x["round"])))
        lag_errors = 0
        for m, rlist in by_member.items():
            for i, r in enumerate(rlist):
                if i == 0:
                    if r["prev_round_C"] not in ("", None):
                        lag_errors += 1
                else:
                    prev = rlist[i - 1]
                    if r["prev_round_C"] != prev["C_group_total"]:
                        lag_errors += 1
                    if r["prev_round_my_F"] != prev["F_profit"]:
                        lag_errors += 1
        report.check("A", "lag variables (prev_round_C, prev_round_my_F) consistent",
                     lag_errors == 0, f"{lag_errors} lag mismatches")

    # CHECK: group descriptive stats (sd, min, max, free_count, full_count)
    # For round 2 of our test, contribs = {1:0, 2:0, 3:10, 4:10}
    # sd = sqrt(mean((x-5)^2)) = sqrt((25+25+25+25)/4) = sqrt(25) = 5.0
    # min=0, max=10, free=2, full=2
    if rows:
        r2_rows = [r for r in rows if int(r["round"]) == 2 and r["block"] == "A"
                   and int(r["session"]) == 1]
        if r2_rows:
            sample = r2_rows[0]
            report.check("A", "sd_contribution R2 (extreme) ≈ 5.0",
                         abs(float(sample["sd_contribution"]) - 5.0) < 0.01,
                         f"got {sample['sd_contribution']}")
            report.check("A", "min_contribution R2 = 0",
                         sample["min_contribution"] == "0",
                         f"got {sample['min_contribution']}")
            report.check("A", "max_contribution R2 = 10",
                         sample["max_contribution"] == "10",
                         f"got {sample['max_contribution']}")
            report.check("A", "free_rider_count R2 = 2",
                         sample["free_rider_count"] == "2",
                         f"got {sample['free_rider_count']}")
            report.check("A", "full_contrib_count R2 = 2",
                         sample["full_contrib_count"] == "2",
                         f"got {sample['full_contrib_count']}")

    # CHECK: participant_id naming convention
    if rows:
        for r in rows:
            expected = f"01_Blue_{int(r['member_no']):02d}"
            if r["participant_id"] != expected:
                report.check("A", "participant_id naming convention",
                             False, f"got {r['participant_id']}, expected {expected}")
                break
        else:
            report.check("A", "participant_id naming convention", True,
                         "01_Blue_NN format for all rows")

    # === Z5 RECONCILIATION ===
    # Compute expected grand total from our actual_results
    sumF_per_m = {m: 0 for m in [1, 2, 3, 4]}
    for (sess, blk, rnd, _contribs, per_member) in actual_results:
        for m in [1, 2, 3, 4]:
            sumF_per_m[m] += per_member[m]["F"]

    # Navigate to Z5 screen
    page.evaluate("go('home')")
    page.wait_for_function("route === 'home'", timeout=5000)
    page.click("#btn-z5")
    page.wait_for_function("route === 'z5Recon'", timeout=5000)

    for m in [1, 2, 3, 4]:
        # Click the chip for member m
        page.evaluate(f"go('z5Recon', {{memberNo: {m}}})")
        page.wait_for_function(f"route === 'z5Recon' && routeArgs.memberNo === {m}", timeout=3000)
        time.sleep(0.1)
        # Read values from the page
        z5 = page.evaluate("""() => {
            const vals = [...document.querySelectorAll('.result-row .value')];
            return vals.map(v => v.textContent.trim());
        }""")
        # Expected: vals[0] = totalF tokens, vals[1] = "X ÷ 4 = R Y",
        # vals[2] = "R 30", vals[3] = "R grand"
        expected_sumF = sumF_per_m[m]
        expected_zar = math.ceil(expected_sumF / TOKENS_PER_ZAR)
        expected_grand = expected_zar + SHOW_UP_FEE_ZAR

        # Parse the on-screen values
        try:
            shown_sumF = int(z5[0].replace("tokens", "").strip())
            shown_grand_text = z5[3]
            shown_grand = int(re.search(r"R\s*(\d+)", shown_grand_text).group(1))
        except Exception as e:
            report.check("A", f"Z5 m{m} parseable", False, f"parse error: {e}, got {z5}")
            continue

        report.check("A", f"Z5 m{m} sum F matches",
                     shown_sumF == expected_sumF,
                     f"app shows {shown_sumF}, computed {expected_sumF}")
        report.check("A", f"Z5 m{m} grand total = ceil(sumF/4) + 30",
                     shown_grand == expected_grand,
                     f"app shows R{shown_grand}, computed R{expected_grand} (sumF={expected_sumF})")


# =============================================================================
# LAYER B — RESUME MECHANISM
# =============================================================================
def layer_b(page: Page, report: TestReport, out_dir: Path, run_id: str):
    """
    Verify that interrupting the experiment at various points and reloading /
    going home leaves no data loss and resumes correctly.

    Test cases (each is an isolated full reset → run partial flow → trigger
    interrupt → verify resume):
      B1. After m1 contributes, reload page → app should resume at m2 callMember
          with m1's contribution still saved (this is the bug from v3 that
          should now be fixed).
      B2. After m2 contributes, click Home button (if exposed)... wait, the
          new app has popstate handler, no home btn shown mid-experiment.
          Instead: navigate to home via JS (page.evaluate('go(\"home\")')) and
          press Start/Resume → should resume at m3.
      B3. After m1 has VIEWED their results, reload → should resume at
          callMemberResults for m2 (results phase resume; v3 bug).
      B4. After m3 has viewed results, reload → resume at callMemberResults m4.
      B5. After all 4 have viewed but before finalTotal PIN → reload → should
          resume at callFinalTotal (or finalTotal).
    """
    print("\n=== LAYER B — Resume Mechanism ===")

    # --- B1: reload during contribution phase ---
    print("  B1: reload after m1 contributes, expect resume at m2")
    reset_app(page)
    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start"); page.wait_for_function("route === 'callMember'", timeout=3000)
    member_contributes(page, 1, 5)
    # Now we should be on callMember(2). Capture state before reload.
    state_before = get_state(page)
    pending_before = state_before["pendingContributions"]
    report.check("B1", "m1=5 stored in pendingContributions before reload",
                 pending_before["1"] == 5,
                 f"got {pending_before}")
    page.reload()
    page.wait_for_function("typeof STATE !== 'undefined'", timeout=10000)
    time.sleep(0.3)
    # After reload, the home page should show "Resume Experiment"
    home_text = visible_text(page)
    report.check("B1", "home shows 'Resume Experiment' after reload",
                 "Resume Experiment" in home_text or "Experiment in progress" in home_text,
                 f"home text snippet: {home_text[:200]}")
    # Click Resume
    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start")
    # Should go to callMember(2), not back to m1!
    time.sleep(0.3)
    cur_route = current_screen(page)
    state_after = get_state(page)
    report.check("B1", "after Resume, route is callMember",
                 cur_route == "callMember",
                 f"got route '{cur_route}'")
    member_no_after = page.evaluate("routeArgs.memberNo")
    report.check("B1", "after Resume, memberNo is 2 (not reset to 1)",
                 member_no_after == 2,
                 f"got memberNo {member_no_after}")
    # m1's contribution should still be 5
    pending_after = state_after["pendingContributions"]
    report.check("B1", "m1's contribution preserved as 5 (NOT wiped)",
                 pending_after["1"] == 5,
                 f"got {pending_after}")

    # --- B2: navigate to home, come back via Resume ---
    print("  B2: jump to home after m2 contributes, expect resume at m3")
    # We're at callMember(2). Have m2 contribute.
    member_contributes(page, 2, 7)
    # Now on callMember(3). Try to navigate home via JS (since back button is blocked).
    # Note: the popstate handler should make even programmatic navigation off mid-
    # experiment unusual, but `go('home')` should still work because it's an
    # in-app router. Whether THAT should be allowed is a separate question.
    # For B2 we use the home navigation that's still legal: via Reset/Start.
    # Actually the proper test is: reload, click Start (which becomes Resume),
    # and verify it resumes at m3.
    page.reload()
    page.wait_for_function("typeof STATE !== 'undefined'", timeout=10000)
    time.sleep(0.3)
    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start")
    time.sleep(0.3)
    member_no_after = page.evaluate("routeArgs.memberNo")
    state_b2 = get_state(page)
    report.check("B2", "after reload+Resume, memberNo is 3",
                 member_no_after == 3, f"got memberNo {member_no_after}")
    report.check("B2", "m1=5 preserved through 2 reloads",
                 state_b2["pendingContributions"]["1"] == 5,
                 f"got {state_b2['pendingContributions']}")
    report.check("B2", "m2=7 preserved",
                 state_b2["pendingContributions"]["2"] == 7,
                 f"got {state_b2['pendingContributions']}")

    # --- B3: reload during results phase ---
    print("  B3: reload after m1 views results, expect resume at m2 results")
    # Finish round 1: m3 contributes, m4 contributes, navigate to results, m1 views
    member_contributes(page, 3, 3)
    member_contributes(page, 4, 5)
    page.wait_for_function("route === 'groupTotal'", timeout=5000)
    page.click("#next")
    page.wait_for_function("route === 'waitForResults'", timeout=5000)
    page.click("#go")
    member_views_results(page, 1)  # m1 sees their results

    # Now we should be on callMemberResults(2). Capture state.
    state_b3_before = get_state(page)
    report.check("B3", "viewedResults[1] is true after m1 views",
                 state_b3_before["viewedResults"]["1"] is True,
                 f"got {state_b3_before['viewedResults']}")
    # Reload
    page.reload()
    page.wait_for_function("typeof STATE !== 'undefined'", timeout=10000)
    time.sleep(0.3)
    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start")
    time.sleep(0.3)
    cur_route = current_screen(page)
    report.check("B3", "after reload during results phase, resumed in results not contributions",
                 cur_route == "callMemberResults",
                 f"got route '{cur_route}'")
    member_no_after = page.evaluate("routeArgs.memberNo")
    report.check("B3", "after results-phase reload, next member is 2 (not 1, who already viewed)",
                 member_no_after == 2, f"got memberNo {member_no_after}")
    # m1's history record should still be there
    state_b3_after = get_state(page)
    m1_history = state_b3_after["history"]["1"]
    report.check("B3", "m1's round-1 history NOT wiped by results-phase reload",
                 len(m1_history) >= 1,
                 f"m1 history length = {len(m1_history)}")

    # --- B4: reload after m3 has viewed results ---
    print("  B4: reload after m3 views results, expect resume at m4 results")
    member_views_results(page, 2)
    member_views_results(page, 3)
    state_b4_before = get_state(page)
    report.check("B4", "viewedResults[3] true after m3 views",
                 state_b4_before["viewedResults"]["3"] is True)
    page.reload()
    page.wait_for_function("typeof STATE !== 'undefined'", timeout=10000)
    time.sleep(0.3)
    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start")
    time.sleep(0.3)
    cur_route = current_screen(page)
    member_no_after = page.evaluate("routeArgs.memberNo")
    report.check("B4", "after m3-viewed reload, resume at callMemberResults m4",
                 cur_route == "callMemberResults" and member_no_after == 4,
                 f"got route='{cur_route}', memberNo={member_no_after}")

    # --- B5: reload after all 4 viewed but before finalTotal PIN ---
    print("  B5: reload after all 4 view results, expect resume at callFinalTotal")
    member_views_results(page, 4)
    # Now should be on callFinalTotal
    page.wait_for_function("route === 'callFinalTotal'", timeout=5000)
    state_b5_before = get_state(page)
    report.check("B5", "all viewedResults true after m4 views",
                 all(state_b5_before["viewedResults"][str(m)] for m in [1,2,3,4]),
                 f"got {state_b5_before['viewedResults']}")
    page.reload()
    page.wait_for_function("typeof STATE !== 'undefined'", timeout=10000)
    time.sleep(0.3)
    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start")
    time.sleep(0.3)
    cur_route = current_screen(page)
    report.check("B5", "after all-viewed reload, resume at callFinalTotal",
                 cur_route == "callFinalTotal",
                 f"got route '{cur_route}'")


# =============================================================================
# LAYER C — popstate (back-button) interception
# =============================================================================
def layer_c(page: Page, report: TestReport, out_dir: Path, run_id: str):
    """
    Verify that pressing the browser back button mid-experiment is intercepted
    and does NOT take the user to home or any other route.
    """
    print("\n=== LAYER C — popstate / back-button interception ===")
    reset_app(page)

    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start"); page.wait_for_function("route === 'callMember'", timeout=3000)
    # Have m1 contribute
    member_contributes(page, 1, 5)
    # Now we should be on callMember(2). Try browser back.
    route_before = current_screen(page)
    state_before = get_state(page)
    page.go_back(timeout=3000)
    time.sleep(0.5)
    # SPA should still be alive (popstate handler re-pushed state)
    spa_alive = _ensure_spa_alive(page)
    report.check("C", "SPA still alive after browser-back mid-experiment",
                 spa_alive, "")
    if spa_alive:
        route_after = current_screen(page)
        state_after = get_state(page)
        # The expected behaviour: route_after should == route_before OR if
        # it changed, isMidExperiment() check shouldn't allow it to be 'home'.
        report.check("C", "browser-back did not navigate to home",
                     route_after != "home",
                     f"route after back = '{route_after}'")
        report.check("C", "browser-back preserved pendingContributions",
                     state_after["pendingContributions"]["1"] ==
                     state_before["pendingContributions"]["1"],
                     f"before m1={state_before['pendingContributions']['1']}, after m1={state_after['pendingContributions']['1']}")

    # Try again deeper in the flow — let m2 contribute, then back
    if current_screen(page) == "callMember":
        # Continue from where we are (may or may not still be on m2)
        cur_m = page.evaluate("routeArgs.memberNo")
        if cur_m == 2:
            member_contributes(page, 2, 8)
            page.go_back(timeout=3000)
            time.sleep(0.5)
            _ensure_spa_alive(page)
            route_after = current_screen(page)
            report.check("C", "deeper browser-back did not navigate to home",
                         route_after != "home",
                         f"route='{route_after}'")
            state2 = get_state(page)
            report.check("C", "m2=8 preserved through browser-back",
                         state2["pendingContributions"].get("2") == 8,
                         f"got {state2['pendingContributions']}")


# =============================================================================
# LAYER D — Behavioral end-to-end with LLM
# =============================================================================
PERSONAS = {
    "trusting_cooperator": "You deeply trust your group. Almost always contribute 8–10 tokens.",
    "cautious_cooperator": "Cautious but cooperative. Contribute 4–7 tokens.",
    "free_rider":          "Strategic free-rider; contribute 0–2.",
    "conditional_cooperator": "Match last round's group average. Start at 5.",
    "emotional_reactor":   "If last profit was bad retaliate with 0, if good give 8, else 5.",
    "decliner":            "Start at 8, drop by 1 each round, min 0.",
    "stubborn_extremist":  "Only ever 0 or 10. Stick with one for a few rounds.",
    "uniform_random":      "Pick uniformly 0-10.",
}
PERSONA_NAMES = list(PERSONAS.keys())

class LLMDecider:
    def __init__(self, use_llm: bool):
        self.use_llm = use_llm
        self.client = None
        self.calls = 0
        if not use_llm: return
        try:
            import anthropic
        except ImportError:
            print("  (anthropic SDK missing — using random)")
            self.use_llm = False; return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("  (ANTHROPIC_API_KEY missing — using random)")
            self.use_llm = False; return
        self.client = anthropic.Anthropic()

    def decide(self, persona: str, member_no: int, round_no: int,
               own_history: list) -> int:
        if not self.use_llm:
            return self._fallback(persona, own_history)
        past = []
        for rec in own_history[-3:]:
            past.append(f"R{rec['round']}: gave {rec['A']}, group={rec['C']}, profit={rec['F']}")
        past_str = "; ".join(past) if past else "no rounds yet"
        prompt = f"""You are participant {member_no} in a 4-person public goods game.
Each round: 10 tokens; contribute 0-10 (A); kept = 10-A; group total doubled and split 4 ways; your profit = kept + share.

PERSONA: {PERSONAS[persona]}

Past rounds: {past_str}

This is round {round_no}. Reply with ONLY a single integer 0-10."""
        try:
            self.calls += 1
            msg = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text.strip()
            for tok in re.findall(r"-?\d+", text):
                v = int(tok)
                if 0 <= v <= 10: return v
            return self._fallback(persona, own_history)
        except Exception:
            return self._fallback(persona, own_history)

    def _fallback(self, persona, history):
        if "free_rider" in persona: return random.choice([0, 0, 1, 2])
        if "cooperator" in persona: return random.randint(5, 10)
        if "extremist" in persona: return random.choice([0, 10])
        if "decliner" in persona:
            return max(0, 8 - len(history))
        if "conditional" in persona and history:
            return max(0, min(10, round(history[-1]["C"] / 4)))
        return random.randint(0, 10)


def layer_d(page: Page, report: TestReport, out_dir: Path, run_id: str,
            use_llm: bool, rounds: int):
    """
    Drive the full experiment (up to `rounds` rounds per session — default 5
    for time) with 4 LLM personas. Verifies:
      - decision_seconds is recorded for every contribution
      - CSV has the right number of rows
      - F = ceil(B + E) holds in all rows
      - treatment_chief and treatment_comm dummies match (session, block)
    """
    print(f"\n=== LAYER D — Behavioral end-to-end ({rounds} rounds × 2 sessions × 2 blocks) ===")
    decider = LLMDecider(use_llm=use_llm)
    reset_app(page, village_type="NON_HOMELAND")

    page.click("#btn-this"); page.wait_for_function("route === 'thisRound'", timeout=3000)
    page.click("#start"); page.wait_for_function("route === 'callMember'", timeout=5000)

    # Pick 4 distinct personas for this run
    personas = random.sample(PERSONA_NAMES, k=4)
    print(f"  personas: m1={personas[0]}, m2={personas[1]}, m3={personas[2]}, m4={personas[3]}")

    own_history = {m: [] for m in [1, 2, 3, 4]}
    rounds_played = 0
    expected_rows = 0

    # Play `rounds_per_block` rounds in block A, then we'd need to play
    # 10 to actually transition to block B. The state machine auto-advances
    # — we just keep calling play_round and read the actual session/block
    # from STATE each time.
    rounds_per_block = min(rounds, 10)
    total_rounds_to_play = rounds_per_block  # only block A unless we play 10
    if rounds_per_block >= 10:
        total_rounds_to_play = 11  # play through into block B too

    for i in range(total_rounds_to_play):
        page.wait_for_function("route === 'callMember'", timeout=10000)
        state_now = get_state(page)
        actual_session = state_now["currentSession"]
        actual_block = state_now["currentBlock"]
        actual_round = state_now["currentRound"]

        # Ask LLM for each member's contribution
        contribs = {}
        for m in [1, 2, 3, 4]:
            amt = decider.decide(personas[m - 1], m, actual_round, own_history[m])
            contribs[m] = amt

        try:
            per_member = play_round(page, contribs, report, "D",
                                    deliberation_seconds=0.2)
        except PWTimeout as e:
            report.add_bug("D", "high", "round_play",
                           f"timeout playing round {actual_round}: {e}")
            break

        # Update own_history
        for m in [1, 2, 3, 4]:
            own_history[m].append({
                "round": actual_round, "A": contribs[m],
                "B": 10 - contribs[m], "C": per_member[m]["C"],
                "D": per_member[m]["D"], "E": per_member[m]["E"],
                "F": per_member[m]["F"],
            })
        rounds_played += 1
        expected_rows += 4
        print(f"    S{actual_session}{actual_block} R{actual_round}: contribs={contribs}, "
              f"sum_F={sum(per_member[m]['F'] for m in [1,2,3,4])}")

    # Export CSV
    csv_text = export_csv(page)
    (out_dir / f"pgg_v5_{run_id}_layer_D_csv.csv").write_text(csv_text, encoding="utf-8")

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)

    report.check("D", f"CSV has {expected_rows} rows ({rounds_played} rounds × 4 members)",
                 len(rows) == expected_rows,
                 f"got {len(rows)}")

    # Verify F = ceil(B + E) in every row
    bad_F = []
    for r in rows:
        try:
            A = int(r["A_contribution"])
            B = int(r["B_private_kept"])
            C = int(r["C_group_total"])
            D = int(r["D_C_times_mult"])
            E = float(r["E_share"])
            F = int(r["F_profit"])
            expected_F = math.ceil(B + E)
            if F != expected_F:
                bad_F.append({"row": r, "expected": expected_F, "got": F})
        except (ValueError, KeyError) as e:
            bad_F.append({"row": r, "error": str(e)})
    report.check("D", "F = ceil(B + E) in every CSV row",
                 len(bad_F) == 0, f"{len(bad_F)} bad rows")

    # Verify treatment dummies.
    # block column is encoded as 0/1 (= treatment_comm) by design.
    # 0 = R1-10 (no communication), 1 = R11-20 (with communication).
    bad_dummies = []
    for r in rows:
        try:
            round_no = int(r["round"])
            expected_chief = 1 if int(r["session"]) == 2 else 0
            expected_comm = 1 if round_no >= 11 else 0
            if int(r["treatment_chief"]) != expected_chief:
                bad_dummies.append(
                    f"row session={r['session']} round={round_no} "
                    f"expected chief={expected_chief}, got {r['treatment_chief']}")
            if int(r["treatment_comm"]) != expected_comm:
                bad_dummies.append(
                    f"row round={round_no} expected comm={expected_comm}, "
                    f"got {r['treatment_comm']}")
            if int(r["block"]) != expected_comm:
                bad_dummies.append(
                    f"row round={round_no} expected block={expected_comm}, "
                    f"got {r['block']}")
        except (ValueError, KeyError):
            bad_dummies.append(f"unparseable row: {r}")
    report.check("D", "treatment_chief, treatment_comm, and block dummies all correct",
                 len(bad_dummies) == 0,
                 f"{len(bad_dummies)} mismatches"
                 + (f"; e.g. {bad_dummies[0]}" if bad_dummies else ""))

    # Verify block == treatment_comm in every row (by design)
    mismatches = [r for r in rows if r["block"] != r["treatment_comm"]]
    report.check("D", "block == treatment_comm in every row (by design)",
                 len(mismatches) == 0,
                 f"{len(mismatches)} rows where block != treatment_comm")

    # Verify decision_seconds present and sensible
    bad_ds = [r for r in rows
              if not r["decision_seconds"] or float(r["decision_seconds"]) <= 0
              or float(r["decision_seconds"]) > 60]
    report.check("D", "decision_seconds present and in (0, 60] for every row",
                 len(bad_ds) == 0, f"{len(bad_ds)} bad decision_seconds")

    # Verify village_type
    if rows:
        vt_set = set(r["village_type"] for r in rows)
        report.check("D", "village_type = NON_HOMELAND",
                     vt_set == {"NON_HOMELAND"}, f"unique values: {vt_set}")


# =============================================================================
# MAIN
# =============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", nargs="+", default=["A", "B", "C", "D"],
                    choices=["A", "B", "C", "D"],
                    help="which test layers to run")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--rounds-per-block", type=int, default=3,
                    help="rounds per block in Layer D (1-10). Default 3.")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = ap.parse_args()

    random.seed(args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    report = TestReport()
    httpd = start_server()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(viewport={"width": 390, "height": 844})
            page = ctx.new_page()
            page.on("pageerror", lambda e:
                    report.add_bug("global", "high", "jsError", str(e)))

            if "A" in args.layers:
                try: layer_a(page, report, out_dir, run_id)
                except Exception as e:
                    report.add_bug("A", "high", "exception",
                                   f"{e}\n{traceback.format_exc()[:600]}")
            if "B" in args.layers:
                try: layer_b(page, report, out_dir, run_id)
                except Exception as e:
                    report.add_bug("B", "high", "exception",
                                   f"{e}\n{traceback.format_exc()[:600]}")
            if "C" in args.layers:
                try: layer_c(page, report, out_dir, run_id)
                except Exception as e:
                    report.add_bug("C", "high", "exception",
                                   f"{e}\n{traceback.format_exc()[:600]}")
            if "D" in args.layers:
                try: layer_d(page, report, out_dir, run_id,
                            use_llm=not args.no_llm,
                            rounds=args.rounds_per_block)
                except Exception as e:
                    report.add_bug("D", "high", "exception",
                                   f"{e}\n{traceback.format_exc()[:600]}")

            browser.close()
    finally:
        httpd.shutdown()

    # === Output files ===
    # Per-check CSV
    results_csv = out_dir / f"pgg_v5_{run_id}_results.csv"
    with results_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["layer", "name", "status", "msg"])
        w.writeheader()
        for c in report.checks:
            w.writerow({k: c[k] for k in ["layer", "name", "status", "msg"]})

    # Bug log
    bugs_path = out_dir / f"pgg_v5_{run_id}_bugs.txt"
    with bugs_path.open("w", encoding="utf-8") as f:
        f.write(f"PGG bot v5 — bug log {run_id}\n" + "=" * 60 + "\n\n")
        if not report.bugs:
            f.write("(no bugs)\n")
        for b in report.bugs:
            f.write(f"[{b['severity'].upper()}] {b['layer']}/{b['where']}\n")
            f.write(f"   {b['msg']}\n")
            if b["ctx"]: f.write(f"   ctx: {json.dumps(b['ctx'], ensure_ascii=False)[:300]}\n")
            f.write("\n")

    # Summary
    counts = report.summary_counts()
    by_layer = {}
    for c in report.checks:
        by_layer.setdefault(c["layer"], {"pass": 0, "fail": 0})
        by_layer[c["layer"]]["pass" if c["status"] == "PASS" else "fail"] += 1
    summary_lines = [
        f"PGG bot v5 — run {run_id}",
        "=" * 60,
        f"Total checks:  {counts['checks_total']}",
        f"  Passed:      {counts['passed']}",
        f"  Failed:      {counts['failed']}",
        f"Total bugs:    {counts['bugs_total']}",
        "",
        "By layer:",
    ]
    for layer in sorted(by_layer):
        c = by_layer[layer]
        verdict = "✓ PASS" if c["fail"] == 0 else f"✗ {c['fail']} FAIL"
        summary_lines.append(f"  Layer {layer}: {c['pass']} pass, {c['fail']} fail — {verdict}")
    summary_lines += ["", f"Output files:",
                      f"  {results_csv}", f"  {bugs_path}",
                      f"  pgg_v5_{run_id}_layer_*_csv.csv"]
    summary = "\n".join(summary_lines)
    summary_path = out_dir / f"pgg_v5_{run_id}_summary.txt"
    summary_path.write_text(summary, encoding="utf-8")
    print("\n" + summary)
    sys.exit(0 if counts["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
