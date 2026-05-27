"""
PGG Field App — Automated participant simulator
================================================

Drives the index.html PWA end-to-end as if 4 participants + 1 facilitator
were using it. Useful for:
  - dry-running the full 40-round experiment before going into the field
  - regression-testing after UI changes
  - stress-testing with different participant "strategies"

Usage:
    python pgg_bot.py [--headless] [--rounds N] [--strategy NAME]

Strategies available:
    cooperator   – always contributes 10
    free_rider   – always contributes 0
    random       – uniform 0..10 each round
    conditional  – matches last round's group average (tit-for-tat-ish)
    mixed        – each of the 4 members uses a different strategy

The bot prints a per-round log and at the end verifies that the in-app
history matches what the bot itself recorded. Exit code 0 = all checks
passed.
"""
import argparse
import random
import re
import sys
import time
import http.server
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, expect, TimeoutError as PWTimeout

APP_DIR = Path(__file__).parent
PORT = 8765
BASE_URL = f"http://localhost:{PORT}/index.html"


# ---------------------------------------------------------------------------
# Tiny static server so the PWA can load with a real http:// origin
# (localStorage + service-worker registration behave better than file://)
# ---------------------------------------------------------------------------
def start_server():
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(APP_DIR), **kw
    )
    # quiet the default logging
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a, **kw: None
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("", PORT), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ---------------------------------------------------------------------------
# Strategies — each returns an integer in [0, 10] for member m in round r
# ---------------------------------------------------------------------------
def strat_cooperator(m, r, history):
    return 10

def strat_free_rider(m, r, history):
    return 0

def strat_random(m, r, history):
    return random.randint(0, 10)

def strat_conditional(m, r, history):
    """Match last round's group AVERAGE contribution; start at 5."""
    if r == 1 or not history[m]:
        return 5
    last = history[m][-1]
    # C is the group total of the last round → avg = C/4
    return max(0, min(10, round(last["C"] / 4)))

def strat_mixed(m, r, history):
    """Member 1 = cooperator, 2 = free-rider, 3 = random, 4 = conditional."""
    return {
        1: strat_cooperator,
        2: strat_free_rider,
        3: strat_random,
        4: strat_conditional,
    }[m](m, r, history)

STRATEGIES = {
    "cooperator": strat_cooperator,
    "free_rider": strat_free_rider,
    "random": strat_random,
    "conditional": strat_conditional,
    "mixed": strat_mixed,
}


# ---------------------------------------------------------------------------
# UI helpers — small wrappers around Playwright's page object
# ---------------------------------------------------------------------------
def click_text(page: Page, text: str, timeout=5000):
    """Click the first button/element whose visible text matches."""
    page.get_by_text(text, exact=False).first.click(timeout=timeout)

def wait_for_text(page: Page, text: str, timeout=5000):
    page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)


# ---------------------------------------------------------------------------
# Step 1 — facilitator setup
# ---------------------------------------------------------------------------
def do_setup(page: Page, pin: str):
    print("  [setup] entering facilitator info…")
    page.fill("#f-name", "TestBot Facilitator")
    page.fill("#f-vname", "TestVillage")
    page.fill("#f-vnum", "1")
    page.fill("#f-pin", pin)
    # date defaults to today, leave it
    # pick Blue group
    page.evaluate("""() => {
        const dots = document.querySelectorAll('.colour-dot');
        for (const d of dots) {
            if (d.dataset.colour === 'Blue') d.click();
        }
    }""")
    page.click("#f-save")
    wait_for_text(page, "This Round")  # home page
    print("  [setup] done.")


# ---------------------------------------------------------------------------
# Step 2 — start a block (Session/Block menu)
# ---------------------------------------------------------------------------
def start_block(page: Page, session: int, block: str):
    print(f"  [block] starting Session {session} · Block {block}")
    page.click("#btn-this")
    click_text(page, "Start")
    # block menu — click the matching list-card
    label = f"Session {session} · Round " + ("1-10" if block == "A" else "11-20")
    click_text(page, label)
    wait_for_text(page, "受試者")  # 'callMember' screen


# ---------------------------------------------------------------------------
# Step 3 — for one round, drive all 4 members through contribution + results
# ---------------------------------------------------------------------------
def enter_pin(page: Page, pin: str):
    """Type the 4-digit pin into the hidden pin input."""
    # The pin input has class 'pin-input' and id 'pin-in'.
    page.wait_for_selector("#pin-in", state="attached", timeout=5000)
    # Focus via the visible 'Tap to type' button — works even though input is offscreen
    page.click("#focus-btn")
    page.fill("#pin-in", pin)
    # the oninput handler auto-advances when length === 4

def member_contribute(page: Page, member_no: int, amount: int, pin: str):
    """One member: call → pin → home → contribute amount → confirm → thanks."""
    # callMember screen
    page.click("#go")
    # pin screen
    enter_pin(page, pin)
    # memberHome — click 'Make this round's contribution'
    page.wait_for_selector("#go", timeout=5000)
    page.click("#go")
    # contribute screen — click the token button with text == amount
    page.wait_for_selector(".token-grid", timeout=5000)
    page.evaluate(
        """(n) => {
            const btns = document.querySelectorAll('.token-btn');
            for (const b of btns) {
                if (b.textContent.trim() === String(n)) { b.click(); return; }
            }
        }""",
        amount,
    )
    page.click("#next")
    # confirm screen
    page.wait_for_selector("#confirm", timeout=5000)
    page.click("#confirm")
    # thanks screen — hand back
    page.wait_for_selector("#ok", timeout=5000)
    page.click("#ok")

def member_view_result(page: Page, member_no: int, pin: str):
    """Results phase per member: call → pin → home → this round → confirm."""
    # callMemberResults — 'Hand phone to participant'
    page.wait_for_selector("#go", timeout=5000)
    page.click("#go")
    enter_pin(page, pin)
    # memberHomeResults — click 'This Round Result'
    page.wait_for_selector("#this", timeout=5000)
    page.click("#this")
    # memberThisRound — read the displayed values, then confirm
    rec = page.evaluate("""() => {
        const rows = document.querySelectorAll('.result-row .value');
        return [...rows].map(r => Number(r.textContent.trim()));
    }""")
    # rows are A, B, C, D, E, F in order
    A, B, C, D, E, F = rec
    page.click("#ok")
    return {"A": A, "B": B, "C": C, "D": D, "E": E, "F": F}


def play_one_round(
    page: Page, round_no: int, contribs: dict, pin: str, log_prefix=""
) -> dict:
    """
    Drives one full round: 4 contributions → group total → 4 result views
    → final total → back to facilitator. Returns the per-member result dict.
    """
    # === Contribution phase ===
    for m in [1, 2, 3, 4]:
        if m > 1:
            wait_for_text(page, "受試者")  # next callMember
        member_contribute(page, m, contribs[m], pin)

    # === Group total page (facilitator-facing) ===
    page.wait_for_selector("#next", timeout=5000)
    # read C and D from the page so we can sanity-check
    group_C = page.evaluate("""() => {
        const boxes = document.querySelectorAll('.confirm-box');
        return Number(boxes[0].textContent.trim());
    }""")
    page.click("#next")

    # === waitForResults ===
    page.wait_for_selector("#go", timeout=5000)
    page.click("#go")  # 'Start with Member 1'

    # === per-member result view ===
    per_member = {}
    for m in [1, 2, 3, 4]:
        if m > 1:
            wait_for_text(page, "受試者")
        per_member[m] = member_view_result(page, m, pin)

    # === Final group total page ===
    page.wait_for_selector("#home", timeout=5000)
    sum_F = sum(per_member[m]["F"] for m in [1, 2, 3, 4])
    page.click("#home")
    wait_for_text(page, "This Round")  # back to facilitator home

    print(
        f"{log_prefix}R{round_no:>2}  contribs={contribs}  "
        f"C={group_C}  ΣF={sum_F}"
    )
    return per_member


# ---------------------------------------------------------------------------
# Verification — compare in-app CSV against what the bot recorded
# ---------------------------------------------------------------------------
def export_and_get_csv(page: Page) -> str:
    page.click("#btn-exp")
    page.wait_for_selector("#preview", timeout=5000)
    csv = page.evaluate("document.getElementById('preview').textContent")
    # back to home
    page.click("#back")
    return csv


def verify(bot_history: dict, csv: str) -> bool:
    """
    bot_history[member][round_index] = {session, block, round, A, B, C, D, E, F}
    csv is the in-app CSV. We re-build a dict from CSV and compare.
    """
    lines = [l for l in csv.strip().splitlines() if l]
    header = lines[0].split(",")
    rows = [dict(zip(header, l.split(","))) for l in lines[1:]]
    by_key = {}
    for r in rows:
        key = (r["participant_id"], int(r["session"]), r["block"], int(r["round"]))
        by_key[key] = r

    ok = True
    for m, recs in bot_history.items():
        for rec in recs:
            pid_suffix = f"_{str(m).zfill(2)}"
            # find matching csv row
            match = [k for k in by_key if k[0].endswith(pid_suffix)
                     and k[1] == rec["session"]
                     and k[2] == rec["block"]
                     and k[3] == rec["round"]]
            if not match:
                print(f"  ✗ MISSING in csv: member {m} S{rec['session']} {rec['block']} R{rec['round']}")
                ok = False
                continue
            row = by_key[match[0]]
            for field in ["A", "B", "C", "D", "E", "F"]:
                csv_val = row[f"{field}_contribution"] if field == "A" else \
                          row[f"{field}_private_kept"] if field == "B" else \
                          row[f"{field}_group_total"] if field == "C" else \
                          row[f"{field}_C_times_mult"] if field == "D" else \
                          row[f"{field}_share"] if field == "E" else \
                          row[f"{field}_profit"]
                if abs(float(csv_val) - float(rec[field])) > 1e-6:
                    print(f"  ✗ MISMATCH m{m} R{rec['round']} {field}: "
                          f"bot={rec[field]} csv={csv_val}")
                    ok = False
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true",
                    help="run without showing the browser")
    ap.add_argument("--rounds", type=int, default=3,
                    help="rounds per block to play (1..10); default 3 for a quick smoke test")
    ap.add_argument("--blocks", type=int, default=1,
                    help="how many blocks to play (1..4); each block is 10 rounds max")
    ap.add_argument("--strategy", choices=list(STRATEGIES), default="mixed")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    pin = "1234"
    strategy_fn = STRATEGIES[args.strategy]

    print(f"PGG bot — strategy={args.strategy}  rounds/block={args.rounds}  blocks={args.blocks}")
    httpd = start_server()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless)
            ctx = browser.new_context(viewport={"width": 390, "height": 844})  # iPhone-ish
            page = ctx.new_page()
            page.goto(BASE_URL)
            # Always start clean
            page.evaluate("localStorage.clear()")
            page.reload()
            wait_for_text(page, "請輸入以下資料")
            do_setup(page, pin)

            # bot's own ledger so we can verify against the app's CSV
            bot_history = {1: [], 2: [], 3: [], 4: []}

            # Block schedule: (session, block) tuples
            schedule = [(1, "A"), (1, "B"), (2, "A"), (2, "B")][: args.blocks]
            for session, block in schedule:
                round_offset = 0 if block == "A" else 10
                for i in range(args.rounds):
                    round_no = round_offset + i + 1
                    contribs = {
                        m: strategy_fn(m, round_no, bot_history) for m in [1, 2, 3, 4]
                    }
                    # Each round, re-enter via This Round → block menu.
                    # The app auto-picks the next uncompleted round, which
                    # matches our round_no.
                    start_block(page, session, block)
                    result = play_one_round(
                        page, round_no, contribs, pin,
                        log_prefix=f"  [S{session}{block}] "
                    )
                    for m in [1, 2, 3, 4]:
                        bot_history[m].append({
                            "session": session, "block": block, "round": round_no,
                            **result[m],
                        })

            print("\n[verify] exporting CSV and comparing…")
            csv = export_and_get_csv(page)
            ok = verify(bot_history, csv)
            if ok:
                print("✓ all rows match — app behaviour verified")
            else:
                print("✗ discrepancies above — investigate")

            browser.close()
            sys.exit(0 if ok else 1)
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
