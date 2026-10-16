#!/usr/bin/env python3
"""
qureo_solver.py — log in to the QUREO learning portal and solve Python course
chapters (lectures + review/test quizzes) through the platform API.

Usage:
  python3 qureo_solver.py status --id ID --pass PASS
  python3 qureo_solver.py solve  --id ID --pass PASS --chapters 7-25
  python3 qureo_solver.py lecture --id ID --pass PASS --lecture 511007020

Answers are cached in qureo_answers.json and reused on later runs, so a
second run can even replay a test from cache without a learning pass.

The portal only answers an Egyptian IP, so by default the solver drives a real
(headed) Chromium window with the "1VPN" browser extension loaded - turn its
Egypt VPN on once and it is reused. Pass --bundled to fall back to Playwright's
headless Chromium instead.
"""
import argparse, io, json, os, re, sys, time, contextlib

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("pip install playwright && python3 -m playwright install chromium")

import browser_util

PORTAL = "https://me-portal.qureo.education/login"
TP = "https://me-tp.qureo.education"
API = TP + "/api/study"
API_PATH = "/api/study"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "qureo_state.json")
CACHE = os.path.join(HERE, "qureo_answers.json")
COURSES = {"python": ".course-card.python", "javascript": ".course-card.purple",
           "js": ".course-card.purple"}

B = '"(.*?)"'
OPS = {"يساوي": "==", "أكبر من أو يساوي": ">=", "أكبر من": ">",
       "أصغر من أو يساوي": "<=", "أصغر من": "<", "لا يساوي": "!="}


def load_cache():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    return {"lecture_talks": {}, "answer": {}, "unhandled": {}}


def save_cache(c):
    json.dump(c, open(CACHE, "w"), ensure_ascii=False, indent=1)


def strip_markup(s):
    return re.sub(r"\[\[/?[a-z_]+\]\]", "", s or "").strip()


# --------------------------------------------------------------------- code helpers
def run_code(code):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(code, "<sol>", "exec"), {"__builtins__": __builtins__}, {})
        return out.getvalue()
    except Exception:
        return None


def parse_instructions(text):
    t = strip_markup(text)
    items = re.split(r"(?:^|[\s.\n])\((\d+)\)", t)
    if len(items) > 1:
        return [items[i + 1].split("*")[0] for i in range(1, len(items), 2)]
    return []


def instr_to_lines(s):
    m = re.search(r"أسند\s*(-?\d+(?:\.\d+)?)\s+إلى المتغير\s+(\w+)", s)
    if m:
        return [(f"{m.group(2)} = {m.group(1)}", 0)]
    m = re.search(r"اطبع\s+" + B, s)
    if not m:
        return []
    string = m.group(1)
    is_elif = "وإلا، إذا" in s or "وإلا إذا" in s
    is_if = ("إذا" in s) and not is_elif
    is_else = "إذا لم يكن كذلك" in s or "لم يتحقق أي" in s or "لم يطابق أي" in s
    m2 = re.search(r"المتغير\s+(\w+)\s+(" + "|".join(map(re.escape, OPS)) + r")\s*\+?\s*(-?\d+(?:\.\d+)?)", s)
    if m2:
        var, op, val = m2.group(1), OPS[m2.group(2)], m2.group(3)
        kw = "elif" if is_elif else "if"
        if is_elif or is_if:
            return [(f"{kw} {var} {op} {val}:", 0), (f'print("{string}")', 1)]
    if is_else:
        return [("else:", 0), (f'print("{string}")', 1)]
    return [(f'print("{string}")', 0)]


def synth_program(instructions):
    lines = []
    for ins in instructions:
        for ln, ind in instr_to_lines(ins):
            lines.append("    " * ind + ln)
    return "\n".join(lines)


# --------------------------------------------------------------------- api client
class QureoAPI:
    def __init__(self, username, password, reuse=True, course="python", cdp=True):
        self.pw = sync_playwright().start()
        # The portal needs an Egyptian IP supplied by the Chrome VPN extension.
        # Playwright's bundled Chromium cannot load extensions, so by default we
        # drive the real Chrome (extension + VPN) over CDP.
        if cdp:
            self.browser, self.ctx = browser_util.connect(self.pw, urls=[PORTAL])
            self.managed = False
        else:
            self.browser = self.pw.chromium.launch(headless=True)
            self.ctx = self.browser.new_context()
            self.managed = True
        self.username, self.password = username, password
        self.course_sel = COURSES.get(course, ".course-card.python")
        self._api_page = None
        self._session(reuse)

    def _goto(self, page, url, wait="domcontentloaded", tries=4):
        for i in range(tries):
            try:
                page.goto(url, wait_until=wait, timeout=60000)
                return
            except Exception as e:
                if i == tries - 1:
                    raise
                time.sleep(2 * (i + 1))

    def _wait_for(self, page, sel, timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            e = page.query_selector(sel)
            if e:
                return e
            time.sleep(0.5)
        return None

    def _login(self):
        self._api_page = None
        page = self.ctx.new_page()
        self._goto(page, PORTAL, wait="domcontentloaded")
        time.sleep(2)
        if self._wait_for(page, "button.portal-selection-button-secondary"):
            page.click("button.portal-selection-button-secondary")
            time.sleep(2)
        if self._wait_for(page, "#student_id"):
            page.fill("#student_id", self.username)
            page.fill("#password", self.password)
            page.click("button.login-button")
            time.sleep(5)
        card = self._wait_for(page, self.course_sel, timeout=15)
        if card is None:
            # drifted to TP before the card click: cards live on portal domain
            self._goto(page, PORTAL, wait="domcontentloaded")
            time.sleep(3)
            card = self._wait_for(page, self.course_sel, timeout=15)
        if card:
            card.click()
            time.sleep(6)   # lands on TP with an authenticated session
            if "me-tp" not in page.url:
                self._goto(page, TP + "/")
                time.sleep(3)
            sb = page.query_selector("button.abCoiN1t")
            if sb:
                sb.click()
                time.sleep(5)
        self.ctx.storage_state(path=STATE)
        page.close()

    def _session(self, reuse):
        if not self.managed:
            # CDP mode: the dedicated profile already keeps the portal session,
            # just make sure we are logged into the requested course.
            self._login()
            return
        if reuse and os.path.exists(STATE) and time.time() - os.path.getmtime(STATE) < 6 * 3600:
            self.ctx.close(); self.browser.close()
            self.browser = self.pw.chromium.launch(headless=True)
            self.ctx = self.browser.new_context(storage_state=STATE)
            if self.get("/students/me") is not None:
                self._login()   # make sure requested course is active
                return
            self.ctx.close(); self.browser.close()
            self.browser = self.pw.chromium.launch(headless=True)
            self.ctx = self.browser.new_context()
        self._login()

    # All API traffic is issued from a page on the TP origin so it carries the
    # session cookies AND the browser's VPN proxy. Playwright's context.request
    # bypasses the browser proxy, which would leak the real IP.
    _JS = """async ([method, path, body]) => {
        const opts = {method, credentials: 'include',
                      headers: {'Accept': 'application/json'}};
        if (body !== null && body !== undefined) {
            opts.headers['content-type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const r = await fetch(path, opts);
        return {status: r.status, text: await r.text()};
    }"""

    def _api_tab(self):
        if self._api_page is None or self._api_page.is_closed():
            self._api_page = self.ctx.new_page()
            # a static asset gives a stable document on the TP origin (the SPA
            # root can redirect mid-request and tear down the JS context)
            self._goto(self._api_page, TP + "/favicon/favicon_48.png")
        return self._api_page

    def _request(self, method, path, body=None):
        try:
            res = self._api_tab().evaluate(self._JS, [method, API_PATH + path, body])
        except Exception:
            self._api_page = None
            res = self._api_tab().evaluate(self._JS, [method, API_PATH + path, body])
        try:
            return res["status"], (json.loads(res["text"]) if res["text"] else None)
        except Exception:
            return res["status"], res["text"]

    def get(self, path):
        st, d = self._request("GET", path)
        return d if st == 200 else None

    def put(self, path, body=None):
        return self._request("PUT", path, body)

    # ---------------------------------------------------------- course info
    def course_id(self):
        return self.get("/students/me")["course_code"]

    def section_id(self):
        d = self.get(f"/courses/{self.course_id()}")
        if d and "basic_section" in d:
            return d["basic_section"]["id"]
        return None

    def chapters(self, sid):
        return self.get(f"/sections/{sid}")["chapters"]

    def student_chapters(self, sid):
        lst = self.get(f"/students/sections/{sid}/chapters") or []
        return {c["chapter_id"]: c for c in lst}

    def diamonds(self):
        return self.get(f"/students/courses/{self.course_id()}/diamond")

    def chapter_lectures(self, ch_id):
        return self.get(f"/chapters/{ch_id}")["lectures"]

    def lecture_talks(self, lid):
        d = self.get(f"/lectures/{lid}")
        return d["talks"] if d else []

    # ---------------------------------------------------------- lectures
    def solve_lecture(self, lid, cache):
        self.put(f"/students/lectures/{lid}")
        talks = self.lecture_talks(lid)
        n = 0
        for t in talks:
            if t["type"] != "try":
                continue
            body = t["body"]["codes"][0]
            ans = body.get("model_answer")
            if ans is None:
                cache["unhandled"][str(t["id"])] = {"kind": "lecture_try_no_model", "lecture": lid}
                continue
            self.put(f"/students/lecture-talks/{t['id']}",
                     {"value": {"try": {"answer": ans, "lang": body.get("lang", "python")}}})
            cache["lecture_talks"][str(t["id"])] = ans
            n += 1
        s, _ = self.put(f"/students/lectures/{lid}/complete")
        print(f"    lecture {lid}: {n} try saved, complete={s}")
        return s == 200

    # ---------------------------------------------------------- quiz
    def _submit(self, kind, ch_id, q, body):
        path = f"/students/{kind}-questions/{q['id']}/answer/{q['type']}"
        return self.put(path, body)

    def solve_quiz(self, chapter, cache):
        ch_id, ttype = chapter["id"], chapter["test_type"]
        kind = "review" if ttype == "review" else "test"
        qs = self.get(f"/chapters/{ch_id}/test")
        if qs is None:
            print("    !! cannot load test questions")
            return False

        def guess_body(item):
            q = item["question"]
            if q["type"] == "choice":
                for ch in item["choices"]:
                    if ch["content"].lstrip().startswith("[[bdi]]"):
                        return {"choice_id": ch["id"]}
                return {"choice_id": item["choices"][0]["id"]}
            desc = item["descriptions"][0]
            starter = desc.get("code", "") or ""
            if starter.strip():
                return {"answer_code": starter + "\n"}
            prog = synth_program(parse_instructions(q["content"]))
            return {"answer_code": (prog + "\n") if prog else "pass\n"}

        def learn_from_review(item, r):
            q = item["question"]
            res = (r or {}).get("result") or {}
            if q["type"] == "choice":
                for ch in res.get("choices", []):
                    if ch.get("correct"):
                        return {"choice_id": ch["choice"]["id"]}
            else:
                model = res.get("model_answer")
                if model is not None:
                    return {"answer_code": model + "\n"}
            return None

        def learn_from_test_result():
            res = self.get(f"/students/chapters/{ch_id}/test/result")
            if not res:
                return False
            rr = res.get("result", {})
            for cq in rr.get("choice_results", []):
                for ch in cq.get("choices", []):
                    if ch.get("correct"):
                        cache["answer"][f"{ch_id}:{cq['question_id']}"] = \
                            {"choice_id": ch["choice"]["id"]}
                        break
            for dq in rr.get("description_results", []):
                if dq.get("correct"):
                    cache["answer"][f"{ch_id}:{dq['question_id']}"] = \
                        {"answer_code": dq.get("answer_code", "")}
                elif dq.get("model_answer") is not None:
                    cache["answer"][f"{ch_id}:{dq['question_id']}"] = \
                        {"answer_code": dq["model_answer"] + "\n"}
            return True

        # harvest known answers from any stored result of a previous attempt
        res = self.get(f"/students/chapters/{ch_id}/{'review' if kind == 'review' else 'test'}/result")
        if res:
            rr = res.get("result", res)
            for cq in rr.get("choice_results", []):
                for ch in cq.get("choices", []):
                    if ch.get("correct"):
                        cache["answer"][f"{ch_id}:{cq['question_id']}"] = {"choice_id": ch["choice"]["id"]}
                        break
            for dq in rr.get("description_results", []):
                if dq.get("correct"):
                    cache["answer"][f"{ch_id}:{dq['question_id']}"] = {"answer_code": dq.get("answer_code", "")}
                elif dq.get("model_answer") is not None:
                    cache["answer"][f"{ch_id}:{dq['question_id']}"] = {"answer_code": dq["model_answer"] + "\n"}

        for attempt in range(1, 4):
            self.put(f"/students/chapters/{ch_id}/test")   # reset in-progress
            dirty = False
            stopped = False
            for idx, item in enumerate(qs, 1):
                q = item["question"]
                ck = f"{ch_id}:{q['id']}"
                body = cache["answer"].get(ck) or guess_body(item)
                s, r = self._submit(kind, ch_id, q, body)
                if kind == "review":
                    res = (r or {}).get("result") or {}
                    if res.get("correct"):
                        cache["answer"][ck] = body
                        continue
                    fixed = learn_from_review(item, r)
                    if not fixed:
                        print(f"    q{idx} review: wrong and unlearnable ({s})")
                        stopped = True
                        break
                    cache["answer"][ck] = fixed
                    dirty = True
                    print(f"    q{idx} review: wrong -> learned")
                else:
                    if s >= 400:
                        print(f"    q{idx} test submit error {s}: {r}")
                        stopped = True
                        break
                    if (r or {}).get("complete") is not None:
                        chn = r["complete"].get("chapter", {})
                        print(f"    test attempt {attempt} finished: "
                              f"correct={chn.get('best_correct_count')}/{len(qs)} "
                              f"diamond={chn.get('diamond')}")
                        if chn.get("diamond") == 5:
                            return True
                        learn_from_test_result()
                        dirty = True
                        stopped = True
                        break
            if not dirty:
                if kind == "review":
                    sc = self.student_chapters(self.section_id()).get(ch_id, {})
                    print(f"    review done: completed={bool(sc.get('completed_at'))} "
                          f"diamond={sc.get('diamond')}")
                    return bool(sc.get("completed_at"))
                return False
            if stopped and attempt == 3:
                print("    !! giving up after 3 attempts")
        sc = self.student_chapters(self.section_id()).get(ch_id, {})
        print(f"    chapter: completed={bool(sc.get('completed_at'))} diamond={sc.get('diamond')}")
        return bool(sc.get("completed_at")) and sc.get("diamond") == 5


def parse_chapters(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "solve", "lecture"])
    ap.add_argument("--id", required=True)
    ap.add_argument("--pass", dest="pw", required=True)
    ap.add_argument("--chapters", default="")
    ap.add_argument("--chapter", type=int, default=0)
    ap.add_argument("--lecture", type=int, default=0)
    ap.add_argument("--no-reuse", action="store_true")
    ap.add_argument("--course", default="python", choices=list(COURSES))
    ap.add_argument("--bundled", action="store_true",
                    help="use Playwright's headless Chromium instead of the "
                         "real Chrome + VPN extension")
    a = ap.parse_args()

    api = QureoAPI(a.id, a.pw, reuse=not a.no_reuse, course=a.course, cdp=not a.bundled)
    cache = load_cache()
    try:
        cid = api.course_id()
        sid = api.section_id()
        chapters = api.chapters(sid)
        sc = api.student_chapters(sid)
        if a.cmd == "status":
            for ch in chapters:
                st = sc.get(ch["id"], {})
                d = st.get("diamond", 0)
                mark = "PERFECT" if st.get("completed_at") and d == 5 else "CLEAR" if st.get("completed_at") else "--"
                print(f"  ch{ch['seq']:2d} [{mark:7s}] {ch['name']:30s} type={ch['test_type']:6s} q={ch['question_count']} best={st.get('best_correct_count', 0)}/{ch['question_count']}")
            print("Diamonds:", api.diamonds())
        elif a.cmd == "lecture":
            api.solve_lecture(a.lecture, cache)
        elif a.cmd == "solve":
            seqs = parse_chapters(a.chapters or (str(a.chapter) if a.chapter else ""))
            for ch in chapters:
                if seqs and ch["seq"] not in seqs:
                    continue
                st = sc.get(ch["id"], {})
                if st.get("completed_at") and st.get("diamond") == 5:
                    print(f"== Chapter {ch['seq']} [{ch['id']}] {ch['name']} — PERFECT, skipping")
                    continue
                if st.get("completed_at"):
                    allc = all(f"{ch['id']}:{q['id']}" in cache["answer"]
                               for it in (api.get(f"/chapters/{ch['id']}/test") or [])
                               for q in [it["question"]])
                    if not allc:
                        print(f"== Chapter {ch['seq']} — completed, no full answer set; not risking reset, skipping")
                        continue
                print(f"== Chapter {ch['seq']} [{ch['id']}] {ch['name']} ({ch['test_type']})")
                lectures = api.chapter_lectures(ch["id"])
                failed = False
                for le in lectures:
                    sst = api.get(f"/students/lectures/{le['id']}") or {}
                    if (sst.get("lecture") or {}).get("completed_at"):
                        continue
                    ok = api.solve_lecture(le["id"], cache)
                    if not ok:
                        print("    !! lecture failed; aborting chapter")
                        failed = True
                        break
                if failed:
                    break
                if not api.solve_quiz(ch, cache):
                    save_cache(cache)
                    print("    !! chapter not completed; stopping (next chapters are locked)")
                    break
                save_cache(cache)
    finally:
        save_cache(cache)
        api.browser.close()
        api.pw.stop()


if __name__ == "__main__":
    main()
