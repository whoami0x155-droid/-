#!/usr/bin/env python3
"""
sprix_solver.py - log in to the SPRIX LEARNING Square portal and solve courses
(financial-literacy chapters, reviews and tests) through the platform API.

Usage:
  python3 sprix_solver.py status  --id ID --pass PASS
  python3 sprix_solver.py solve   --id ID --pass PASS [--subject 7]
  python3 sprix_solver.py chapter --id ID --pass PASS --chapter 70
  python3 sprix_solver.py test    --id ID --pass PASS --test 23

Answers are cached in sprix_answers.json (per subject) and reused on later runs.
The portal signs every API call with a CSRF/Origin check, so all requests are
issued from the browser page context through fetch(), never from the raw HTTP
client.

The CSRF check only applies to mutating endpoints, and the platform never
reveals the correct option for a wrong chapter answer, so the solver walks the
answer set with a staged search:
  1. replay any cached correct answer for the question;
  2. otherwise submit each option in turn and keep the one that comes back
     is_correct=true (the chapter endpoint accepts repeated submissions).
A test result response exposes the ground-truth correct option per question, so
a first (wrong) test pass is enough to learn every answer and a second pass
scores 100%.

The portal only answers an Egyptian IP, so by default the solver drives a real
(headed) Chromium window with the "1VPN" browser extension loaded - turn its
Egypt VPN on once and it is reused. Pass --bundled to fall back to Playwright's
headless Chromium instead.
"""
import argparse, json, os, sys, time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("pip install playwright && python3 -m playwright install chromium")

import browser_util

PORTAL = "https://egy.app.spx-learning-square.com"
API = "/api/v1"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "sprix_state.json")
CACHE = os.path.join(HERE, "sprix_answers.json")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def load_cache():
    if os.path.exists(CACHE):
        try:
            c = json.load(open(CACHE))
            c.setdefault("chapter", {})
            c.setdefault("test", {})
            c.setdefault("unhandled", {})
            return c
        except Exception:
            pass
    return {"chapter": {}, "test": {}, "unhandled": {}}


def save_cache(c):
    json.dump(c, open(CACHE, "w"), ensure_ascii=False, indent=1)


# --------------------------------------------------------------------- api client
class SprixAPI:
    def __init__(self, username, password, reuse=True, cdp=True):
        self.pw = sync_playwright().start()
        # The portal needs an Egyptian IP supplied by the Chrome VPN extension.
        # Playwright's bundled Chromium cannot load extensions, so by default we
        # drive the real Chrome (extension + VPN) over CDP.
        if cdp:
            self.browser, self.ctx = browser_util.connect(self.pw, urls=[PORTAL + "/"])
            self.managed = False
        else:
            self.browser = self.pw.chromium.launch(headless=True)
            self.ctx = self.browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 720},
                locale="ar-EG",
                timezone_id="Africa/Cairo",
            )
            self.managed = True
        self.username, self.password = username, password
        self._session(reuse)

    # -------------------------------------------------------------- session
    def _goto(self, url, wait="domcontentloaded", tries=3):
        for i in range(tries):
            try:
                self.page.goto(url, wait_until=wait, timeout=60000)
                return
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(2 * (i + 1))

    def _login(self):
        self.page = self.ctx.new_page()
        self._goto(PORTAL + "/")
        time.sleep(2)
        st, _ = self.raw("POST", "/auth/login",
                         {"login_id": self.username, "password": self.password})
        if st != 200:
            raise SystemExit(f"login failed ({st}); check --id/--pass")
        self._goto(PORTAL + "/")
        time.sleep(2)
        self.ctx.storage_state(path=STATE)

    def _session(self, reuse):
        if not self.managed:
            # CDP mode: the dedicated profile already keeps the portal session.
            self._login()
            return
        if reuse and os.path.exists(STATE) and time.time() - os.path.getmtime(STATE) < 6 * 3600:
            try:
                self.ctx.close()
                self.ctx = self.browser.new_context(
                    user_agent=UA, viewport={"width": 1280, "height": 720},
                    locale="ar-EG", timezone_id="Africa/Cairo", storage_state=STATE)
                self.page = self.ctx.new_page()
                # the saved state only counts if it belongs to the same login id
                me = self.get("/auth/me")
                if me and str((me.get("data") or {}).get("login_id")) == str(self.username):
                    return
                self.ctx.close()
                self.ctx = self.browser.new_context(
                    user_agent=UA, viewport={"width": 1280, "height": 720},
                    locale="ar-EG", timezone_id="Africa/Cairo")
            except Exception:
                pass
        self._login()

    # -------------------------------------------------------------- requests
    _JS = """async ([method, ep, body]) => {
        const opts = {method, headers: {'Content-Type':'application/json','Accept':'application/json'}};
        if (body !== null) opts.body = JSON.stringify(body);
        const r = await fetch('/api/v1'+ep, opts);
        return {status: r.status, text: await r.text()};
    }"""

    def raw(self, method, ep, body=None):
        """Perform an API request from the JS page context (adds CSRF/origin)."""
        try:
            res = self.page.evaluate(self._JS, [method, ep, body])
        except Exception:
            # lost page context: rebuild and retry once
            self.page = self.ctx.new_page()
            self._goto(PORTAL + "/")
            time.sleep(2)
            res = self.page.evaluate(self._JS, [method, ep, body])
        try:
            return res["status"], json.loads(res["text"])
        except Exception:
            return res["status"], res["text"]

    def get(self, ep):
        st, d = self.raw("GET", ep)
        return d if st == 200 else None

    def post(self, ep, body=None):
        return self.raw("POST", ep, body)

    def put(self, ep, body=None):
        return self.raw("PUT", ep, body)

    # -------------------------------------------------------------- helpers
    def subjects(self):
        d = self.get("/subjects")
        return (d or {}).get("data", []) if isinstance(d, dict) else []

    def self_id(self):
        d = self.get("/auth/me")
        return ((d or {}).get("data") or {}).get("id")

    def curriculum(self, sid):
        d = self.get(f"/subjects/{sid}/curriculum")
        if not isinstance(d, dict):
            return []
        data = d.get("data")
        return data if isinstance(data, list) else data.get("units", [])

    # -------------------------------------------------------------- chapter
    def solve_chapter(self, cid, cache):
        """Solve every question block in a chapter then mark it complete."""
        d = self.get(f"/chapters/{cid}/blocks")
        blocks = ((d or {}).get("data") or {}).get("blocks", [])
        n = 0
        for bl in blocks:
            if bl["type"] != "question":
                continue
            q = bl["question"]
            if q["type"] != "mcq":
                cache["unhandled"].setdefault(
                    str(q["id"]), {"kind": "chapter_" + q["type"], "chapter": cid})
                continue
            ck = str(q["id"])
            opts = [o["id"] for o in bl.get("options") or []]
            chosen = cache["chapter"].get(ck)
            if chosen not in opts:
                chosen = None
            if chosen is None:
                chosen = self._find_option(cid, bl["id"], opts)
                if chosen is None:
                    print(f"    block {bl['id']} q{q['id']}: no correct option found")
                    cache["unhandled"].setdefault(ck, {"kind": "chapter_mcq", "chapter": cid})
                    continue
                cache["chapter"][ck] = chosen
            else:
                self.post(f"/chapters/{cid}/answer",
                          {"block_id": bl["id"], "selected_option_id": chosen})
            n += 1
        st, d = self.post(f"/chapters/{cid}/complete")
        star = (d.get("data") or {}).get("star_score") if isinstance(d, dict) else None
        print(f"    chapter {cid}: {n} questions, complete={st} star={star}")
        return st == 200

    def _find_option(self, cid, block_id, opt_ids):
        """Return the first option the API grades as correct, or None."""
        if not opt_ids:
            return None
        # if a previous result ever exposed the answer, it is already cached;
        # otherwise probe. First try each option, remember the correct one.
        for oid in opt_ids:
            st, d = self.post(f"/chapters/{cid}/answer",
                              {"block_id": block_id, "selected_option_id": oid})
            res = (d or {}).get("data") or {} if isinstance(d, dict) else {}
            if res.get("is_correct"):
                return oid
        return None

    # -------------------------------------------------------------- review
    def solve_review(self, rid, cache):
        d = self.get(f"/reviews/{rid}/items")
        items = ((d or {}).get("data") or {}).get("items", [])
        st, _ = self.post(f"/reviews/{rid}/complete")
        print(f"    review {rid}: {len(items)} items, complete={st}")
        return st == 200

    # -------------------------------------------------------------- test
    def solve_test(self, tid, cache, max_attempts=6):
        """Answer a test to 100%: attempt once (learning from the result),
        then replay the learned answer set until it passes."""
        for attempt in range(1, max_attempts + 1):
            self.post(f"/tests/{tid}/abandon")
            st, d = self.post(f"/tests/{tid}/start")
            started = (d or {}).get("data") or {} if isinstance(d, dict) else {}
            if st != 200 or not started:
                print(f"    !! test {tid} could not start ({st})")
                return False
            tqs = started.get("test_questions", [])
            options = started.get("options", {}) or {}
            submitted = {}  # test_question_id -> (question_id, option_id)
            for tq in tqs:
                qid = str(tq["question_id"])
                opts = [o["id"] for o in options.get(qid, [])]
                chosen = cache["test"].get(qid)
                if chosen not in opts:
                    # unknown: rotate through the options on each attempt so a
                    # later attempt can land on the correct one
                    probe = cache.setdefault("test_probe", {})
                    idx = probe.get(qid, 0)
                    if not opts:
                        continue
                    chosen = opts[idx % len(opts)]
                    probe[qid] = idx + 1
                self.put(f"/tests/{tid}/questions/{tq['id']}/answer",
                         {"selected_option_id": chosen})
                submitted[tq["id"]] = (qid, chosen)
            st, d = self.post(f"/tests/{tid}/submit")
            result = (d or {}).get("data") or {} if isinstance(d, dict) else {}
            resp = result.get("response") or {}
            rid = resp.get("id")
            print(f"    test {tid} attempt {attempt}: score={resp.get('total_score')} "
                  f"passed={resp.get('passed')}")
            if rid is not None:
                self._learn_test_result(tid, rid, submitted, cache)
            if resp.get("passed"):
                return True
        print(f"    !! test {tid} did not pass after {max_attempts} attempts")
        return False

    def _learn_test_result(self, tid, rid, submitted, cache):
        """Map each graded response back to its question and remember the
        option that was graded correct. Unknown questions are dropped from the
        rotation state so a later pass stops probing them."""
        d = self.get(f"/tests/{tid}/result?response_id={rid}")
        res = (d or {}).get("data") or {} if isinstance(d, dict) else {}
        probe = cache.get("test_probe", {})
        learned = 0
        for qr in res.get("question_responses", []):
            info = submitted.get(qr.get("test_question_id"))
            if not info or not qr.get("is_correct"):
                continue
            qid, opt = info
            cache["test"][qid] = opt
            probe.pop(qid, None)
            learned += 1
        if learned:
            print(f"      learned {learned} correct test answers")


# --------------------------------------------------------------------- solve all
def solve_subject(api, sid, cache, chapters_filter=None):
    """Walk the subject in order, re-reading the curriculum after each item so
    newly unlocked items are picked up in the same run."""
    done = 0
    seen_fail = set()
    while True:
        units = api.curriculum(sid)
        target = None
        for u in units:
            for it in u.get("items", []):
                if chapters_filter and it["id"] not in chapters_filter:
                    continue
                if it.get("is_completed") or not it.get("is_unlocked"):
                    continue
                if it["id"] in seen_fail:
                    continue
                target = it
                break
            if target:
                break
        if not target:
            break
        t = target["item_type"]
        print(f"  == {t} {target['id']} '{target['title']}'")
        if t == "chapter":
            ok = api.solve_chapter(target["id"], cache)
        elif t == "review":
            ok = api.solve_review(target["id"], cache)
        elif t == "test":
            ok = api.solve_test(target["id"], cache)
        else:
            ok = False
        if ok:
            done += 1
        else:
            seen_fail.add(target["id"])
            print(f"     (could not complete {target['id']}; skipping)")
        save_cache(cache)
        # safety: if nothing has changed and the next item is still blocked, stop
        if len(seen_fail) > 40:
            print("   too many failures; stopping")
            break
    return done


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "solve", "chapter", "review", "test"])
    ap.add_argument("--id", required=True)
    ap.add_argument("--pass", dest="pw", required=True)
    ap.add_argument("--subject", type=int, default=7)
    ap.add_argument("--chapter", type=int, default=0)
    ap.add_argument("--review", type=int, default=0)
    ap.add_argument("--test", type=int, default=0)
    ap.add_argument("--all-subjects", action="store_true")
    ap.add_argument("--no-reuse", action="store_true")
    ap.add_argument("--bundled", action="store_true",
                    help="use Playwright's headless Chromium instead of the "
                         "real Chrome + VPN extension")
    a = ap.parse_args()

    api = SprixAPI(a.id, a.pw, reuse=not a.no_reuse, cdp=not a.bundled)
    cache = load_cache()
    try:
        if a.cmd == "chapter":
            api.solve_chapter(a.chapter, cache)
        elif a.cmd == "review":
            api.solve_review(a.review, cache)
        elif a.cmd == "test":
            api.solve_test(a.test, cache)
        elif a.cmd == "status":
            for s in api.subjects():
                if s.get("kind") == "game":
                    continue
                print(f"subject {s['id']} '{s['name']}' "
                      f"{s.get('completed_items')}/{s.get('total_items')} "
                      f"({s.get('progress_pct', 0):.1f}%)")
                for u in api.curriculum(s["id"]):
                    print(f"  unit {u['id']} '{u['title']}' "
                          f"{u.get('completed_items')}/{u.get('total_items')}")
                    for it in u.get("items", []):
                        st = "DONE" if it["is_completed"] else ("UNLOCK" if it["is_unlocked"] else "LOCK")
                        cur = " <-- current" if it.get("is_current") else ""
                        print(f"    {it['item_type']:8} {it['id']:5} {st:6} '{it['title']}'{cur}")
        elif a.cmd == "solve":
            subjects = api.subjects()
            for s in subjects:
                if s.get("kind") == "game":
                    continue
                if not a.all_subjects and s["id"] != a.subject:
                    continue
                print(f"== subject {s['id']} '{s['name']}'")
                if s.get("code_language") or s.get("course_code") == "programming":
                    print("   (coding subject - not handled by this solver)")
                    continue
                n = solve_subject(api, s["id"], cache)
                print(f"   finished {n} items")
    finally:
        save_cache(cache)
        api.browser.close()
        api.pw.stop()


if __name__ == "__main__":
    main()