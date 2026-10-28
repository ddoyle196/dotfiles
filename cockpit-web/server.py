#!/usr/bin/env python3
"""Local web view of the conversation cockpit.

Serves index.html and a small JSON API over the same registry the tmux panel
reads (`cc-host.sh list`), plus the facts only transcripts carry: when Daniel
last typed into a conversation, and the checkpoints it passed through.

Bound to 127.0.0.1. Every API call needs the token baked into the page, and a
Host header naming this port, so another site open in the browser can neither
read the state nor type into a conversation.

    python3 server.py            serve on http://localhost:7477
    python3 server.py --check    self-test against a stubbed cc-host.sh
"""
import argparse
import calendar
import errno
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PORT = 7477
HERE = os.path.dirname(os.path.realpath(__file__))
HOME = os.path.expanduser("~")

LIST_TTL = 3
WORKING_WINDOW = 2 * 3600
WORKING_MAX = 6
ATTENTION_MAX = 3
SINCE_AWAY_MAX = 3
AWAY_AFTER = 30 * 60
JUST_FINISHED = 5 * 60
WAITING_LONG = 3 * 86400
STALE_DECISION = 3 * 86400
TEXT_MAX = 90
REPLY_MAX = 2000
SEND_MAX = 20000
CHECKPOINTS_MAX = 40
BODY_MAX = 1 << 20
CHUNK = 4 << 20
TRANSCRIPT_MISS_TTL = 60
# \Z, not $: $ also matches before a trailing newline.
CONV_ID = re.compile(r"^cc_[0-9a-f]{1,32}\Z")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}\Z")
TOKEN_MARK = "__COCKPIT_TOKEN__"
NONCE_MARK = "__COCKPIT_NONCE__"


def clip(text, n=TEXT_MAX):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def epoch(ts):
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


# ---------------------------------------------------------------- transcripts

def typed_text(obj):
    """What Daniel typed on this line, or None when it is not a typed message."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isCompactSummary"):
        return None
    if "toolUseResult" in obj:
        return None
    content = (obj.get("message") or {}).get("content")
    text = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                break
    if not text or not text.strip():
        return None
    head = text.lstrip()
    # Slash commands, hook output and resume caveats arrive as user lines too.
    if head.startswith("<") or head.startswith("Caveat:"):
        return None
    return text


def typed_at(line):
    # Cheap byte tests first: most lines are tool traffic, and json.loads on a
    # multi-megabyte tool result is where a naive scan spends its time.
    if b'"type":"user"' not in line or b'"toolUseResult"' in line:
        return 0
    try:
        obj = json.loads(line)
    except ValueError:
        return 0
    if not isinstance(obj, dict) or typed_text(obj) is None:
        return 0
    return epoch(obj.get("timestamp"))


def scan_forward(path, start, on_line, chunk=CHUNK):
    """Feed complete lines from `start` to on_line; return the offset after the
    last newline, so a line still being written is read whole next time."""
    offset = start
    carry = b""
    with open(path, "rb") as fh:
        fh.seek(start)
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            buf = carry + buf
            cut = buf.rfind(b"\n")
            if cut < 0:
                carry = buf
                continue
            for line in buf[:cut].split(b"\n"):
                if line:
                    on_line(line)
            # offset already sits at the start of the carried partial line, and
            # cut is measured from there.
            offset += cut + 1
            carry = buf[cut + 1:]
    return offset


def last_typed_backward(path, end):
    """Newest typed-message time before byte `end`, reading from the back, so a
    100MB transcript costs only as much as its tail on first sight."""
    pos, carry = end, b""
    with open(path, "rb") as fh:
        while pos > 0:
            step = min(1 << 18, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + carry).split(b"\n")
            carry = lines[0]
            for line in reversed(lines[1:]):
                at = typed_at(line)
                if at:
                    return at
    return typed_at(carry) if carry else 0


def complete_end(path, size):
    """Offset just past the last newline at or before `size`."""
    with open(path, "rb") as fh:
        pos = size
        while pos > 0:
            step = min(1 << 16, pos)
            pos -= step
            fh.seek(pos)
            cut = fh.read(step).rfind(b"\n")
            if cut >= 0:
                return pos + cut + 1
    return 0


def plan_title(plan):
    for line in (plan or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return clip(line, 200)
    return "plan"


class Transcript:
    """Incremental reads of one transcript. Two independent cursors: the cheap
    one behind /api/state, and the full pass a detail view needs."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.user_off = None
        self.user_at = 0
        self.detail_off = 0
        self.detail_sig = None
        self.checkpoints = []
        self.events = 0
        self.reply = {"at": 0, "text": ""}

    def last_user_at(self):
        with self.lock:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                return self.user_at
            # The file can vanish between the size check and the read; keep the
            # last answer rather than fail the whole refresh.
            try:
                self._advance_user(size)
            except OSError:
                pass
            return self.user_at

    def _advance_user(self, size):
        if self.user_off is None or size < self.user_off:
            end = complete_end(self.path, size)
            self.user_at = last_typed_backward(self.path, end)
            self.user_off = end
        elif size > self.user_off:
            newest = [self.user_at]

            def on_line(line):
                at = typed_at(line)
                if at > newest[0]:
                    newest[0] = at

            self.user_off = scan_forward(self.path, self.user_off, on_line)
            self.user_at = newest[0]

    def detail(self):
        with self.lock:
            try:
                st = os.stat(self.path)
            except OSError:
                return self._detail_view()
            sig = (st.st_size, st.st_mtime)
            if sig != self.detail_sig:
                if st.st_size < self.detail_off:
                    self.detail_off, self.checkpoints, self.events = 0, [], 0
                    self.reply = {"at": 0, "text": ""}
                try:
                    self.detail_off = scan_forward(self.path, self.detail_off, self._detail_line)
                    self.detail_sig = sig
                except OSError:
                    pass
            return self._detail_view()

    def _detail_view(self):
        return {
            "checkpoints": [dict(c) for c in self.checkpoints[-CHECKPOINTS_MAX:]],
            "total_events": self.events,
            "last_reply": dict(self.reply),
        }

    def _add_checkpoint(self, kind, at, text):
        last = self.checkpoints[-1] if self.checkpoints else None
        if last and last["kind"] == kind and last["text"] == text:
            last["n"] += 1
            last["at"] = at or last["at"]
            return
        self.checkpoints.append({"kind": kind, "at": at, "text": text, "n": 1})

    def _detail_line(self, line):
        self.events += 1
        wanted = (b'"away_summary"' in line or b'"compact_boundary"' in line
                  or b'"pr-link"' in line or b'"ExitPlanMode"' in line
                  or (b'"type":"assistant"' in line and b'"type":"text"' in line))
        if not wanted:
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        kind, sub = obj.get("type"), obj.get("subtype")
        at = epoch(obj.get("timestamp"))
        if kind == "system" and sub == "away_summary":
            text = str(obj.get("content") or "").replace("(disable recaps in /config)", "")
            self._add_checkpoint("recap", at, text.strip())
        elif kind == "system" and sub == "compact_boundary":
            trigger = (obj.get("compactMetadata") or {}).get("trigger") or ""
            self._add_checkpoint("compact", at, ("compacted (%s)" % trigger) if trigger else "compacted")
        elif kind == "pr-link":
            repo = str(obj.get("prRepository") or "").split("/")[-1]
            self._add_checkpoint("pr", at, "%s#%s" % (repo, obj.get("prNumber")))
        elif kind == "assistant" and not obj.get("isSidechain"):
            content = (obj.get("message") or {}).get("content")
            if not isinstance(content, list):
                return
            text = None
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "ExitPlanMode":
                    self._add_checkpoint("plan", at, plan_title((block.get("input") or {}).get("plan")))
                elif block.get("type") == "text" and (block.get("text") or "").strip():
                    text = block["text"]
            if text is not None:
                self.reply = {"at": at, "text": text[:REPLY_MAX]}


# ---------------------------------------------------------------- the model

class Cockpit:
    def __init__(self, host_cmd, state_dir, projects, index):
        self.host_cmd = host_cmd
        self.state_dir = state_dir
        self.projects = projects
        self.index = index
        os.makedirs(state_dir, exist_ok=True)
        self.ws_path = os.path.join(state_dir, "working-set.json")
        self.visit_path = os.path.join(state_dir, "visit.json")
        self.list_lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.ws_lock = threading.Lock()
        self.visit_lock = threading.Lock()
        self.entries = None
        self.listed_at = 0.0
        self.refreshing = False
        self.paths = {}
        self.missing = {}
        self.transcripts = {}

    # -- conversation list --------------------------------------------------

    def host(self, *args, timeout=30):
        return subprocess.run([self.host_cmd] + [str(a) for a in args],
                              capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def refresh(self):
        """Run `list` and warm the transcript cursors. Serialised: the command
        rewrites registry files, so two at once would only race each other."""
        with self.refresh_lock:
            try:
                self._refresh()
            # Any failure, not just the expected ones: a flag left set would
            # stop every later refresh and freeze the page on old data.
            except Exception as err:
                sys.stderr.write("cockpit-web: refresh failed: %r\n" % err)
            finally:
                with self.list_lock:
                    self.refreshing = False

    def _refresh(self):
        try:
            proc = self.host("list", timeout=30)
            entries = json.loads(proc.stdout)
            if not isinstance(entries, list):
                raise ValueError("list did not return an array")
        except (OSError, ValueError, subprocess.SubprocessError) as err:
            sys.stderr.write("cockpit-web: list failed: %s\n" % err)
            return
        for e in entries:
            self._last_user_at(e)
        with self.list_lock:
            self.entries = entries
            self.listed_at = time.time()

    def current(self):
        with self.list_lock:
            entries, age, busy = self.entries, time.time() - self.listed_at, self.refreshing
            if entries is not None and age > LIST_TTL and not busy:
                self.refreshing = True
                threading.Thread(target=self.refresh, daemon=True).start()
        if entries is None:
            self.refresh()
            with self.list_lock:
                entries = self.entries
        return entries or []

    def invalidate(self):
        with self.list_lock:
            self.listed_at = 0.0

    # -- transcripts --------------------------------------------------------

    def transcript_for(self, sid):
        if not sid:
            return None
        path = self.paths.get(sid)
        if not path or not os.path.exists(path):
            # A gone transcript would otherwise cost a glob over every project
            # on every poll; a new one is picked up once the miss expires.
            if time.time() - self.missing.get(sid, 0) < TRANSCRIPT_MISS_TTL:
                return None
            path = load_json(os.path.join(self.index, sid + ".json"), {}).get("transcript")
            if not path or not os.path.exists(path):
                found = glob.glob(os.path.join(self.projects, "*", sid + ".jsonl"))
                path = found[0] if found else None
            if not path:
                self.missing[sid] = time.time()
                return None
            self.missing.pop(sid, None)
            self.paths[sid] = path
        t = self.transcripts.get(path)
        if t is None:
            t = self.transcripts.setdefault(path, Transcript(path))
        return t

    def _last_user_at(self, e):
        t = self.transcript_for(e.get("session_id") or "")
        return t.last_user_at() if t else 0

    # -- shaping ------------------------------------------------------------

    def conv(self, e, now):
        state = e.get("state") or "pickup"
        running = state == "running"
        updated = e.get("updated_at") or 0
        unread = bool(e.get("unread"))
        return {
            "id": e["id"],
            "label": e.get("label") or "",
            "topic": e.get("topic") or "",
            "state": state,
            "recap": e.get("recap") or "",
            "question": e.get("question") or "",
            "waiting_suggest": e.get("waiting_suggest") or "",
            "unread": unread,
            "updated_at": updated,
            "last_user_at": self._last_user_at(e),
            "created": e.get("created") or 0,
            "cold": bool(e.get("cold")),
            "running": running,
            "blocked_on": e.get("blocked_on") or "",
            "blocked_since": e.get("blocked_since") or 0,
            "archived": bool(e.get("archived")),
            "prs": [{"label": p.get("label", ""), "mark": p.get("mark", "")}
                    for p in e.get("prs") or []],
            "just_finished": (not running and unread and 0 <= now - updated < JUST_FINISHED),
        }

    def convs(self, now):
        out = [self.conv(e, now) for e in self.current() if e.get("id")]
        out.sort(key=lambda c: -c["updated_at"])
        return out

    def find(self, cid, now):
        for c in self.convs(now):
            if c["id"] == cid:
                return c
        return None

    def state(self):
        now = time.time()
        entries = self.current()
        convs = self.convs(now)
        ws = self.working_set(convs, now)
        return {
            "generated_at": int(now),
            "counts": counts(convs),
            "working_set": ws,
            "attention": attention(convs, ws, now),
            "since_away": self.since_away(convs),
            "convs": convs,
            "topics": topics(entries, convs),
        }

    def working_set(self, convs, now):
        by = {c["id"]: c for c in convs}

        def qualifies(c):
            return not c["archived"] and (c["running"] or now - c["last_user_at"] <= WORKING_WINDOW)

        with self.ws_lock:
            prev = [i for i in load_json(self.ws_path, []) if isinstance(i, str)]
            # A slot is held while it qualifies, so the strip on the page does
            # not reshuffle every time a different conversation gets a message.
            order = [i for i in prev if i in by and qualifies(by[i])]
            fresh = sorted((c for c in convs if qualifies(c) and c["id"] not in order),
                           key=lambda c: -c["last_user_at"])
            order += [c["id"] for c in fresh]
            if len(order) > WORKING_MAX:
                top = set(sorted(order, key=lambda i: -by[i]["last_user_at"])[:WORKING_MAX])
                order = [i for i in order if i in top]
            if order != prev:
                save_json(self.ws_path, order)
            return order

    def visit(self):
        now = time.time()
        with self.visit_lock:
            v = load_json(self.visit_path, {})
            last = v.get("last") or 0
            away = last if last and now - last > AWAY_AFTER else 0
            snapshot = {c["id"]: pr_marks(c) for c in self.convs(now)}
            save_json(self.visit_path, {
                "last": int(now),
                "away_since": int(away),
                # PR marks as they stood at the visit being compared against.
                "away_prs": v.get("prs", {}) if away else {},
                "prs": snapshot,
            })
        return away

    def since_away(self, convs):
        v = load_json(self.visit_path, {})
        away = v.get("away_since") or 0
        if not away:
            return []
        before = v.get("away_prs") or {}
        picks = []
        for c in convs:
            if c["archived"]:
                continue
            spoke = c["updated_at"] > away
            pr_moved = c["id"] in before and before[c["id"]] != pr_marks(c)
            if not (spoke or pr_moved):
                continue
            rank = 0 if spoke and c["state"] == "answer" else (1 if pr_moved else 2)
            picks.append((rank, -c["updated_at"], c))
        picks.sort(key=lambda p: (p[0], p[1]))
        return [{"id": c["id"], "what": clip("%s: %s" % (c["label"], c["question"] or c["recap"]))}
                for _r, _u, c in picks[:SINCE_AWAY_MAX]]

    def tidy(self):
        now = time.time()
        entries = self.current()
        convs = self.convs(now)
        busy = set(self.working_set(convs, now))
        done, stale = [], []
        for c in convs:
            if c["archived"]:
                continue
            evidence = done_evidence(c)
            # Merged PRs alone do not end a conversation he is still typing in,
            # or one he marked as waiting on someone.
            if evidence and c["state"] != "done" and (c["id"] in busy or c["blocked_on"]):
                evidence = ""
            if evidence:
                done.append({"id": c["id"], "label": c["label"], "evidence": evidence})
            idle = now - max(c["updated_at"], c["last_user_at"])
            if c["question"] and idle > STALE_DECISION:
                stale.append({"id": c["id"], "label": c["label"], "question": c["question"],
                              "age_days": int(idle // 86400)})
        return {"probably_done": done, "empty_topics": empty_topics(entries),
                "stale_decisions": stale}

    def detail(self, cid):
        now = time.time()
        c = self.find(cid, now)
        if c is None:
            return None
        entry = next((e for e in self.current() if e.get("id") == cid), {})
        t = self.transcript_for(entry.get("session_id") or "")
        extra = t.detail() if t else {"checkpoints": [], "total_events": 0,
                                       "last_reply": {"at": 0, "text": ""}}
        c.update(extra)
        return c


def pr_marks(c):
    return ["%s:%s" % (p["label"], p["mark"]) for p in c["prs"]]


def counts(convs):
    n = {"need_you": 0, "working": 0, "waiting": 0, "idle": 0, "done": 0, "filed": 0,
         "total": len(convs)}
    for c in convs:
        s = c["state"]
        if c["archived"]:
            n["filed"] += 1
        elif s == "answer" and not c["blocked_on"]:
            n["need_you"] += 1
        elif s == "running":
            n["working"] += 1
        elif s == "waiting":
            n["waiting"] += 1
        elif s in ("done", "dead"):
            n["done"] += 1
        else:
            n["idle"] += 1
    return n


def attention(convs, working_set, now):
    taken, out = set(working_set), []
    live = [c for c in convs if not c["archived"]]

    def add(c, kind, reason, ev_kind, ref):
        if len(out) >= ATTENTION_MAX or c["id"] in taken:
            return
        taken.add(c["id"])
        out.append({"id": c["id"], "kind": kind, "reason": clip(reason),
                    "evidence": {"kind": ev_kind, "ref": ref}})

    for c in live:
        if c["state"] == "answer" and not c["blocked_on"]:
            if c["question"]:
                add(c, "asked", c["question"], "question", c["question"])
            else:
                add(c, "asked", c["recap"], "recap", c["recap"])
    for c in live:
        failing = next((p for p in c["prs"] if p["mark"] == "fail"), None)
        if failing:
            add(c, "failed_check", "%s checks are failing" % failing["label"], "pr", failing["label"])
    for c in sorted(live, key=lambda c: c["blocked_since"]):
        since = c["blocked_since"]
        if c["blocked_on"] and since and now - since > WAITING_LONG:
            days = int((now - since) // 86400)
            add(c, "waiting_long", "waiting on %s for %dd" % (c["blocked_on"], days),
                "blocked", c["blocked_on"])
    decisions = [c for c in live if c["state"] == "pickup" and c["question"] and c["id"] not in taken]
    if decisions:
        c = min(decisions, key=lambda c: c["updated_at"])
        add(c, "decision", c["question"], "question", c["question"])
    return out


def done_evidence(c):
    if c["state"] == "answer" or c["running"]:
        return ""
    # An open or failing PR means the work is not over, whatever the label says.
    if any(p["mark"] != "merged" for p in c["prs"]):
        return ""
    pr_words = ""
    if c["prs"]:
        pr_words = {1: "PR merged", 2: "both PRs merged"}.get(len(c["prs"]), "all PRs merged")
    if c["state"] == "done":
        return "marked done" + ("; " + pr_words if pr_words else "")
    return pr_words


def empty_topics(entries):
    return [e.get("topic") for e in entries if not e.get("id") and e.get("topic")]


def topics(entries, convs):
    count = {}
    for c in convs:
        if not c["archived"] and c["topic"]:
            count[c["topic"]] = count.get(c["topic"], 0) + 1
    empty = set(empty_topics(entries))
    names = sorted(set(count) | empty, key=str.lower)
    return [{"name": n, "count": count.get(n, 0), "empty": n in empty} for n in names]


# ---------------------------------------------------------------- HTTP

class ApiError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


ACTIONS = {"send", "resume", "file", "unfile", "block", "unblock", "seen"}


class Handler(BaseHTTPRequestHandler):
    server_version = "cockpit"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 10

    def log_message(self, fmt, *args):
        if self.server.verbose:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    @property
    def cockpit(self):
        return self.server.cockpit

    def _send(self, code, body, ctype="application/json; charset=utf-8", csp=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        # One request per connection. A rejected request's unread body would
        # otherwise be parsed as the next request on a reused socket, where it
        # borrows the Host and token headers of whatever the page sends next.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def _fail(self, code, message):
        self._send(code, {"ok": False, "error": message})

    def _guard(self, api):
        """Host is checked on every request: a DNS-rebound name would otherwise
        reach the page, and with it the token."""
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            raise ApiError(403, "This server only answers to localhost.")
        if api:
            given = self.headers.get("X-Cockpit-Token", "")
            if not secrets.compare_digest(given.encode(), self.server.token.encode()):
                raise ApiError(403, "Missing or wrong cockpit token.")

    def _body(self):
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(403, "Requests must be sent as JSON.")
        length = self._length()
        if length is None:
            raise ApiError(400, "The request length is not valid.")
        if length > BODY_MAX:
            raise ApiError(400, "The request is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        self.body_read = True
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "The request is not valid JSON.")
        if not isinstance(body, dict):
            raise ApiError(400, "The request must be a JSON object.")
        return body

    def _length(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        return length if length >= 0 else None

    def _drain(self):
        """Read a rejected request's body before answering, so closing the
        socket does not reset it under the response the client is reading."""
        length = self._length()
        if getattr(self, "body_read", False) or not length or length > BODY_MAX:
            return
        try:
            self.rfile.read(length)
        except OSError:
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            self._guard(api=path.startswith("/api/"))
            if path in ("/", "/index.html"):
                return self._page()
            if path == "/api/state":
                return self._send(200, self.cockpit.state())
            if path == "/api/tidy":
                return self._send(200, self.cockpit.tidy())
            m = re.match(r"^/api/conv/([^/]+)$", path)
            if m:
                found = self.cockpit.detail(m.group(1)) if CONV_ID.match(m.group(1)) else None
                if found is None:
                    raise ApiError(404, "No conversation with that id.")
                return self._send(200, found)
            raise ApiError(404, "Nothing here.")
        except ApiError as err:
            self._fail(err.code, str(err))

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            self._guard(api=True)
            body = self._body()
            if path == "/api/visit":
                self.cockpit.visit()
                return self._send(200, {"ok": True})
            if path == "/api/tidy":
                return self._send(200, self._tidy(body))
            m = re.match(r"^/api/conv/([^/]+)/([a-z]+)$", path)
            if not m or m.group(2) not in ACTIONS:
                raise ApiError(404, "Nothing here.")
            return self._send(200, self._act(m.group(1), m.group(2), body))
        except ApiError as err:
            self._drain()
            self._fail(err.code, str(err))

    def _known(self, cid):
        if not CONV_ID.match(cid) or self.cockpit.find(cid, time.time()) is None:
            raise ApiError(404, "No conversation with that id.")

    def _run(self, args, timeout=30):
        try:
            proc = self.cockpit.host(*args, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ApiError(409, "The cockpit took too long to %s." % args[0])
        except OSError as err:
            raise ApiError(409, "Could not run the cockpit: %s." % err.strerror)
        if proc.returncode != 0:
            why = " ".join((proc.stderr or "").split())[:200]
            raise ApiError(409, "The cockpit could not %s%s" % (args[0], (": " + why) if why else "."))

    def _act(self, cid, action, body):
        self._known(cid)
        if action == "send":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ApiError(400, "There is no message to send.")
            if len(text) > SEND_MAX:
                raise ApiError(400, "The message is too long to send.")
            # A parked conversation is started first and given time to boot.
            self._run(["reply", cid, text], timeout=90)
        elif action == "resume":
            self._run(["start", cid])
        elif action == "file":
            self._run(["archive", cid])
        elif action == "unfile":
            self._run(["unarchive", cid])
        elif action == "block":
            who, days = body.get("who"), body.get("days", 0)
            if not isinstance(who, str) or not who.strip() or len(who) > 80:
                raise ApiError(400, "Say who the conversation is waiting on.")
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 365:
                raise ApiError(400, "Days must be a whole number from 0 to 365.")
            self._run(["block", cid, who.strip(), days])
        elif action == "unblock":
            self._run(["unblock", cid])
        elif action == "seen":
            self._run(["seen", cid])
        self.cockpit.invalidate()
        return {"ok": True}

    def _tidy(self, body):
        ids, names = body.get("file", []), body.get("drop_topics", [])
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ApiError(400, "file must be a list of conversation ids.")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ApiError(400, "drop_topics must be a list of topic names.")
        known = {c["id"] for c in self.cockpit.convs(time.time())}
        unknown = [i for i in ids if i not in known or not CONV_ID.match(i)]
        if unknown:
            raise ApiError(404, "No conversation with id %s." % unknown[0])
        empty = set(empty_topics(self.cockpit.current()))
        busy = [n for n in names if n not in empty]
        if busy:
            raise ApiError(400, "Topic %s still has conversations in it." % busy[0])
        filed = dropped = 0
        try:
            for cid in ids:
                self._run(["archive", cid])
                filed += 1
            for name in names:
                self._run(["topic-rm", name])
                dropped += 1
        finally:
            self.cockpit.invalidate()
        return {"ok": True, "filed": filed, "dropped": dropped}

    def _page(self):
        try:
            with open(self.server.page, encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            raise ApiError(503, "index.html is missing next to server.py.")
        nonce = secrets.token_urlsafe(18)
        html = html.replace(TOKEN_MARK, self.server.token).replace(NONCE_MARK, nonce)
        # The page holds the token; if an escaping slip ever lets markup in,
        # this keeps it from running script or sending the token elsewhere.
        csp = ("default-src 'none'; script-src 'nonce-%s'; "
               "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % nonce)
        self._send(200, html.encode(), "text/html; charset=utf-8", csp=csp)


def read_token(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
        with open(path) as fh:
            token = fh.read().strip()
        if TOKEN_RE.match(token):
            return token
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    token = secrets.token_hex(16)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    return token


def make_server(cockpit, token, port, page, verbose=False):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    bound = httpd.server_address[1]
    httpd.allowed_hosts = {"localhost:%d" % bound, "127.0.0.1:%d" % bound}
    httpd.cockpit = cockpit
    httpd.token = token
    httpd.page = page
    httpd.verbose = verbose
    return httpd


def default_cockpit():
    return Cockpit(
        host_cmd=os.environ.get("COCKPIT_HOST_CMD") or os.path.join(HOME, ".tmux/scripts/cc-host.sh"),
        state_dir=os.path.join(HOME, ".claude/cockpit/web"),
        projects=os.path.join(HOME, ".claude/projects"),
        index=os.path.join(HOME, ".claude/session-index"),
    )


# ---------------------------------------------------------------- self-test

STUB = r'''#!%s
import json, sys
here = %r
with open(here + "/calls.log", "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["list"]:
    sys.stdout.write(open(here + "/list.json").read())
'''


def _line(obj):
    # Compact like Claude Code writes them: the byte prefilters depend on it.
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def _typed(t, text):
    return _line({"type": "user", "message": {"role": "user", "content": text}, "timestamp": _iso(t)})


def check():
    import http.client
    results = []

    def ok(name, cond, detail=""):
        results.append(bool(cond))
        print("%s  %s%s" % ("PASS" if cond else "FAIL", name, ("  (" + detail + ")") if detail and not cond else ""))

    root = tempfile.mkdtemp(prefix="cockpit-check-")
    now = time.time()
    try:
        proj = os.path.join(root, "projects", "-tmp")
        os.makedirs(proj)
        os.makedirs(os.path.join(root, "index"))
        stub = os.path.join(root, "cc-host-stub")
        with open(stub, "w") as fh:
            fh.write(STUB % (sys.executable, root))
        os.chmod(stub, 0o755)
        page = os.path.join(root, "index.html")
        with open(page, "w") as fh:
            fh.write("<html><script nonce='%s'>const T='%s'</script></html>" % (NONCE_MARK, TOKEN_MARK))

        def entry(cid, **kw):
            e = {"id": cid, "session": cid, "topic": "t", "label": cid, "recap": "recap " + cid,
                 "unread": False, "prs": [], "state": "pickup", "session_id": "sid-" + cid,
                 "blocked_on": "", "blocked_since": 0, "wake_at": 0, "archived": False,
                 "updated_at": int(now - 7200), "created": int(now - 86400 * 9), "cold": True,
                 "question": "", "waiting_suggest": ""}
            e.update(kw)
            return e

        fixture = [
            entry("cc_a1", state="answer", question="Ship it now?", unread=True,
                  updated_at=int(now - 60), topic="hub"),
            entry("cc_b2", state="answer", question="Option 1 or option 2?", updated_at=int(now - 18000)),
            entry("cc_c3", prs=[{"label": "repo#1", "mark": "fail"}], updated_at=int(now - 86400)),
            entry("cc_d4", state="waiting", blocked_on="Pat", blocked_since=int(now - 5 * 86400)),
            entry("cc_e5", question="Pick A or B?", updated_at=int(now - 6 * 86400)),
            entry("cc_f6", state="done", prs=[{"label": "repo#2", "mark": "merged"},
                                              {"label": "repo#3", "mark": "merged"}]),
            entry("cc_g7", archived=True),
            entry("cc_h8", state="running", cold=False, session_id=""),
            {"id": "", "session": "", "topic": "later", "label": "no conversations yet", "recap": "",
             "state": "empty", "session_id": "", "updated_at": 0, "created": 0, "cold": True,
             "unread": False, "prs": [], "archived": False, "blocked_on": "", "blocked_since": 0,
             "wake_at": 0},
        ]

        def write_fixture():
            save_json(os.path.join(root, "list.json"), fixture)

        write_fixture()
        a_path = os.path.join(proj, "sid-cc_a1.jsonl")
        with open(a_path, "w") as fh:
            fh.write(_typed(now - 600, "please fix the panel"))
            fh.write(_line({"type": "system", "subtype": "away_summary",
                            "content": "Fixed the panel. (disable recaps in /config)", "timestamp": _iso(now - 590)}))
            fh.write(_line({"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
                            "compactMetadata": {"trigger": "auto"}, "timestamp": _iso(now - 580)}))
            for dt in (570, 560):
                fh.write(_line({"type": "pr-link", "prNumber": 7, "prRepository": "org/repo",
                                "timestamp": _iso(now - dt)}))
            fh.write(_line({"type": "assistant", "timestamp": _iso(now - 550), "message": {"content": [
                {"type": "tool_use", "name": "ExitPlanMode", "input": {"plan": "# Rebuild the strip\n- step"}}]}}))
            fh.write(_line({"type": "assistant", "timestamp": _iso(now - 540), "message": {"content": [
                {"type": "text", "text": "Done. Ship it now?"}]}}))
            # Newer than the typed message, and none of them were typed by him.
            fh.write(_typed(now - 300, "<command-name>/clear</command-name>"))
            fh.write(_typed(now - 290, "Caveat: the messages below were generated"))
            fh.write(_line({"type": "user", "isMeta": True, "message": {"content": "meta"},
                            "timestamp": _iso(now - 280)}))
            fh.write(_line({"type": "user", "toolUseResult": {}, "message": {"content": [
                {"type": "text", "text": "tool said"}]}, "timestamp": _iso(now - 270)}))
        with open(os.path.join(proj, "sid-cc_b2.jsonl"), "w") as fh:
            fh.write(_typed(now - 18000, "start"))
        c_path = os.path.join(proj, "sid-cc_c3.jsonl")
        with open(c_path, "w") as fh:
            fh.write(_typed(now - 86400, "check the build"))
        with open(os.path.join(proj, "sid-cc_g7.jsonl"), "w") as fh:
            fh.write(_typed(now - 300, "archived but recent"))

        cockpit = Cockpit(stub, os.path.join(root, "state"), os.path.join(root, "projects"),
                          os.path.join(root, "index"))
        token = read_token(os.path.join(root, "token"))
        ok("token file is mode 600 and 32 hex chars",
           oct(os.stat(os.path.join(root, "token")).st_mode & 0o777) == "0o600"
           and re.match(r"^[0-9a-f]{32}$", token))
        loose = os.path.join(root, "loose-token")
        with open(loose, "w") as fh:
            fh.write("short\n")
        os.chmod(loose, 0o644)
        fixed = read_token(loose)
        ok("a loose or malformed token file is repaired",
           TOKEN_RE.match(fixed) and os.stat(loose).st_mode & 0o777 == 0o600)
        ok("conversation ids reject a trailing newline", not CONV_ID.match("cc_a1\n"))
        ok("done_evidence refuses open or failing PRs",
           [done_evidence({"state": "done", "running": False, "prs": [{"label": "r#1", "mark": m}]})
            for m in ("fail", "open")] == ["", ""]
           and done_evidence({"state": "done", "running": False, "prs": []}) == "marked done")

        big = os.path.join(root, "big.jsonl")
        with open(big, "w") as fh:
            for i in range(500):
                fh.write(_line({"i": i, "pad": "x" * (i % 37)}))
        seen = []
        off = scan_forward(big, 0, seen.append, chunk=97)
        with open(big, "a") as fh:
            fh.write(_line({"i": 500}))
        off2 = scan_forward(big, off, seen.append, chunk=97)
        ok("forward scan across many chunks reads each line once",
           len(seen) == 501 and off2 == os.path.getsize(big)
           and [json.loads(x)["i"] for x in seen] == list(range(501)), "%d lines, %d/%d" % (len(seen), off2, os.path.getsize(big)))
        httpd = make_server(cockpit, token, 0, page)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def call(method, path, body=None, host=None, tok=token, ctype="application/json",
                 raw_headers=None, want=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            headers = {"Host": host or "localhost:%d" % port}
            if tok is not None:
                headers["X-Cockpit-Token"] = tok
            data = None
            if method == "POST":
                data = json.dumps(body if body is not None else {}).encode()
                headers["Content-Type"] = ctype
            headers.update(raw_headers or {})
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            if want:
                return resp.status, raw.decode(errors="replace"), resp.getheader(want)
            try:
                return resp.status, json.loads(raw), resp.getheader("Access-Control-Allow-Origin")
            except ValueError:
                return resp.status, raw.decode(errors="replace"), resp.getheader("Access-Control-Allow-Origin")

        def calls():
            try:
                with open(os.path.join(root, "calls.log")) as fh:
                    return [json.loads(x) for x in fh if x.strip()]
            except OSError:
                return []

        # -- guards
        st, body, csp = call("GET", "/", tok=None, want="Content-Security-Policy")
        ok("GET / substitutes the token", st == 200 and token in body and TOKEN_MARK not in body)
        nonce = re.search(r"nonce='([^']+)'", body)
        ok("GET / sends a CSP whose script nonce matches the page",
           nonce and NONCE_MARK not in body and csp and ("'nonce-%s'" % nonce.group(1)) in csp
           and "default-src 'none'" in csp, str(csp))
        ok("GET / with a foreign Host is 403", call("GET", "/", host="evil.example:%d" % port)[0] == 403)
        ok("state without token is 403", call("GET", "/api/state", tok=None)[0] == 403)
        ok("state with wrong token is 403", call("GET", "/api/state", tok="0" * 32)[0] == 403)
        ok("state with foreign Host is 403", call("GET", "/api/state", host="evil.example")[0] == 403)
        ok("Host 127.0.0.1 is accepted", call("GET", "/api/state", host="127.0.0.1:%d" % port)[0] == 200)
        ok("POST as text/plain is 403",
           call("POST", "/api/conv/cc_a1/seen", ctype="text/plain")[0] == 403)
        ok("POST without token is 403", call("POST", "/api/visit", tok=None)[0] == 403)
        st, _b, _c = call("POST", "/api/visit", raw_headers={"Content-Length": "-1"})
        ok("negative Content-Length is 400", st == 400, str(st))

        import socket
        smuggled = (b"POST /api/conv/cc_a1/file HTTP/1.1\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 0\r\nX-Swallow: ")
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"POST /api/visit HTTP/1.1\r\nHost: localhost:%d\r\nContent-Type: text/plain\r\n"
                     b"Content-Length: %d\r\n\r\n%s" % (port, len(smuggled), smuggled))
        first = b""
        while b"\r\n\r\n" not in first:
            part = sock.recv(4096)
            if not part:
                break
            first += part
        try:
            sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: localhost:%d\r\nX-Cockpit-Token: %s\r\n\r\n"
                         % (port, token.encode()))
            sock.settimeout(2)
            while sock.recv(65536):
                pass
        except OSError:
            pass
        sock.close()
        time.sleep(0.2)
        ok("a rejected body cannot ride the next request on the socket",
           first.startswith(b"HTTP/1.1 403") and b"Connection: close" in first
           and ["archive", "cc_a1"] not in calls(), first[:40].decode(errors="replace"))
        ok("guards never ran a host action", not [c for c in calls() if c[0] != "list"])

        # -- state
        st, s, cors = call("GET", "/api/state")
        ok("state is 200 with no CORS header", st == 200 and cors is None)
        ok("counts", s["counts"] == {"need_you": 2, "working": 1, "waiting": 1, "idle": 2, "done": 1,
                                     "filed": 1, "total": 8}, json.dumps(s["counts"]))
        ok("working set is recent typing plus running, archived left out",
           s["working_set"] == ["cc_a1", "cc_h8"], json.dumps(s["working_set"]))
        by = {c["id"]: c for c in s["convs"]}
        ok("last_user_at ignores commands, caveats, meta and tool results",
           abs(by["cc_a1"]["last_user_at"] - int(now - 600)) <= 1, str(by["cc_a1"]["last_user_at"]))
        ok("just_finished set only for the fresh unread one",
           [c["id"] for c in s["convs"] if c["just_finished"]] == ["cc_a1"])
        kinds = [(a["id"], a["kind"]) for a in s["attention"]]
        ok("attention order asked, failed_check, waiting_long; capped at 3",
           kinds == [("cc_b2", "asked"), ("cc_c3", "failed_check"), ("cc_d4", "waiting_long")], json.dumps(kinds))
        ok("waiting_long reason", s["attention"][2]["reason"] == "waiting on Pat for 5d", s["attention"][2]["reason"])
        ok("attention never repeats the working set", not set(s["working_set"]) & {a["id"] for a in s["attention"]})
        ok("empty topic reported, not a conversation",
           {"name": "later", "count": 0, "empty": True} in s["topics"] and "" not in by)
        ok("since_away empty before any visit", s["since_away"] == [])

        fixture[2]["prs"] = []
        write_fixture()
        cockpit.refresh()
        kinds = [(a["id"], a["kind"]) for a in call("GET", "/api/state")[1]["attention"]]
        ok("one oldest decision fills the last slot",
           kinds == [("cc_b2", "asked"), ("cc_d4", "waiting_long"), ("cc_e5", "decision")], json.dumps(kinds))

        # -- since_away
        ok("first visit records", call("POST", "/api/visit")[0] == 200)
        ok("since_away empty right after a visit", call("GET", "/api/state")[1]["since_away"] == [])
        v = load_json(cockpit.visit_path, {})
        v["last"] = int(now - 3600)
        v["prs"]["cc_f6"] = ["repo#2:open", "repo#3:merged"]
        save_json(cockpit.visit_path, v)
        call("POST", "/api/visit")
        away = call("GET", "/api/state")[1]["since_away"]
        ok("since_away after an hour: the ask first, then the PR change",
           [x["id"] for x in away] == ["cc_a1", "cc_f6"] and away[0]["what"] == "cc_a1: Ship it now?",
           json.dumps(away))

        # -- tidy
        st, t, _ = call("GET", "/api/tidy")
        ok("tidy proposes the done one with evidence",
           t["probably_done"] == [{"id": "cc_f6", "label": "cc_f6", "evidence": "marked done; both PRs merged"}],
           json.dumps(t["probably_done"]))
        ok("tidy lists empty topics", t["empty_topics"] == ["later"])
        ok("tidy lists stale decisions",
           [(x["id"], x["age_days"]) for x in t["stale_decisions"]] == [("cc_e5", 6)], json.dumps(t["stale_decisions"]))
        ok("tidy proposes only: nothing filed by reading it", not [c for c in calls() if c[0] != "list"])

        # -- actions, all against the stub
        before = len(calls())
        posts = [
            ("/api/conv/cc_a1/send", {"text": "yes, ship"}, ["reply", "cc_a1", "yes, ship"]),
            ("/api/conv/cc_a1/resume", {}, ["start", "cc_a1"]),
            ("/api/conv/cc_a1/file", {}, ["archive", "cc_a1"]),
            ("/api/conv/cc_a1/unfile", {}, ["unarchive", "cc_a1"]),
            ("/api/conv/cc_d4/block", {"who": "Pat", "days": 2}, ["block", "cc_d4", "Pat", "2"]),
            ("/api/conv/cc_d4/unblock", {}, ["unblock", "cc_d4"]),
            ("/api/conv/cc_a1/seen", {}, ["seen", "cc_a1"]),
        ]
        for path, payload, argv in posts:
            st, r, _ = call("POST", path, payload)
            ok("%s runs %s" % (path.rsplit("/", 1)[1], " ".join(argv)),
               st == 200 and r == {"ok": True} and argv in calls()[before:], "%s %s" % (st, r))
        n = len([c for c in calls() if c[0] != "list"])
        st, r, _ = call("POST", "/api/conv/cc_zz/send", {"text": "hi"})
        ok("unknown id is 404 with a sentence", st == 404 and r["ok"] is False and r["error"])
        ok("empty message is 400", call("POST", "/api/conv/cc_a1/send", {"text": "  "})[0] == 400)
        ok("bad days is 400", call("POST", "/api/conv/cc_d4/block", {"who": "Pat", "days": "x"})[0] == 400)
        ok("rejected requests ran nothing", len([c for c in calls() if c[0] != "list"]) == n)
        ok("tidy refuses a topic that has conversations",
           call("POST", "/api/tidy", {"file": [], "drop_topics": ["hub"]})[0] == 400)
        st, r, _ = call("POST", "/api/tidy", {"file": ["cc_f6"], "drop_topics": ["later"]})
        ok("tidy files and drops", st == 200 and r == {"ok": True, "filed": 1, "dropped": 1}
           and ["archive", "cc_f6"] in calls() and ["topic-rm", "later"] in calls(), json.dumps(r))

        # -- detail
        st, d, _ = call("GET", "/api/conv/cc_a1")
        cps = [(c["kind"], c["text"], c["n"]) for c in d.get("checkpoints", [])]
        ok("checkpoints in order, duplicates collapsed",
           cps == [("recap", "Fixed the panel.", 1), ("compact", "compacted (auto)", 1),
                   ("pr", "repo#7", 2), ("plan", "Rebuild the strip", 1)], json.dumps(cps))
        ok("last_reply is the last assistant text", d["last_reply"]["text"] == "Done. Ship it now?")
        ok("total_events counts lines", d["total_events"] == 11, str(d["total_events"]))
        ok("unknown conv detail is 404", call("GET", "/api/conv/cc_nope")[0] == 404)

        # -- incremental read and slot stability
        with open(c_path, "a") as fh:
            fh.write(_typed(now - 5, "half a line")[:-20])
        s = call("GET", "/api/state")[1]
        ok("a half-written line is not read yet",
           {c["id"]: c for c in s["convs"]}["cc_c3"]["last_user_at"] == int(now - 86400))
        with open(c_path, "a") as fh:
            fh.write(_typed(now - 5, "half a line")[-20:])
        s = call("GET", "/api/state")[1]
        ok("its completion is picked up incrementally",
           {c["id"]: c for c in s["convs"]}["cc_c3"]["last_user_at"] == int(now - 5))
        ok("a newer conversation appends instead of jumping the queue",
           s["working_set"] == ["cc_a1", "cc_h8", "cc_c3"], json.dumps(s["working_set"]))
        fixture[2]["prs"] = [{"label": "repo#9", "mark": "merged"}]
        fixture[7]["state"] = "pickup"
        write_fixture()
        cockpit.refresh()
        ok("one that stops qualifying leaves its slot",
           call("GET", "/api/state")[1]["working_set"] == ["cc_a1", "cc_c3"])
        ok("merged PRs alone do not propose a conversation still in the working set",
           "cc_c3" not in [x["id"] for x in call("GET", "/api/tidy")[1]["probably_done"]])

        real = cockpit._last_user_at

        def boom(_e):
            raise FileNotFoundError("gone")

        cockpit._last_user_at = boom
        cockpit.refresh()
        cockpit._last_user_at = real
        ok("a failed refresh does not leave the refresh flag stuck", cockpit.refreshing is False)

        # -- speed
        times = []
        for _ in range(20):
            t0 = time.time()
            call("GET", "/api/state")
            times.append((time.time() - t0) * 1000)
        ok("warm /api/state under 300ms (max %.1fms)" % max(times), max(times) < 300)
        httpd.shutdown()
        httpd.server_close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    failed = results.count(False)
    print("%d passed, %d failed" % (len(results) - failed, failed))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description="Local web view of the conversation cockpit.")
    ap.add_argument("--check", action="store_true", help="run the self-test and exit")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--verbose", action="store_true", help="log each request")
    args = ap.parse_args()
    if args.check:
        sys.exit(check())
    cockpit = default_cockpit()
    token = read_token(os.path.join(HOME, ".claude/cockpit/web-token"))
    try:
        httpd = make_server(cockpit, token, args.port, os.path.join(HERE, "index.html"), args.verbose)
    except OSError as err:
        if err.errno != errno.EADDRINUSE:
            raise
        # Exit cleanly so the login agent does not restart into the same wall
        # every few seconds while something else holds the port.
        print("Port %d is already in use." % args.port, file=sys.stderr, flush=True)
        sys.exit(0)
    threading.Thread(target=cockpit.refresh, daemon=True).start()
    print("cockpit: http://localhost:%d" % args.port, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
