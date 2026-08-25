"""Render the conversation registry as JSON, folding in whatever is running.

Reads three blocks on stdin (AGENTS / PS / PANES) so the shell pays for one
python start instead of a jq per field per conversation.
"""
import json, os, sys, time

REG, IDX = sys.argv[1], sys.argv[2]
COCKPIT = os.path.dirname(REG.rstrip("/"))
TOPICS = sys.argv[3] if len(sys.argv) > 3 else ""

# Which PRs each conversation touched, and how they are doing. Harvested from
# the transcripts by cc-harvest.py; absent files just mean no badges.
def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default

PR_REFS = _load(os.path.join(COCKPIT, "prs.json"), {})
PR_STATE = _load(os.path.join(COCKPIT, "pr-status.json"), {})

def pr_badges(tid):
    """Only PRs the conversation worked on, best-attributed first."""
    entry = PR_REFS.get(tid) or {}
    prs = entry.get("prs") or {}
    scored = []
    for key, v in prs.items():
        w = v.get("w", 0) if isinstance(v, dict) else 0
        at = v.get("at", 0) if isinstance(v, dict) else 0
        if w < 2:                     # mentioned, not worked on
            continue
        scored.append((key, w, at))
    if not scored:
        return []
    def rank(t):
        key, w, at = t
        st = PR_STATE.get(key) or {}
        open_first = 0 if st.get("state") == "OPEN" else 1
        return (-w, open_first, -at)
    out = []
    for key, _w, _at in sorted(scored, key=rank)[:2]:
        st = PR_STATE.get(key) or {}
        repo, num = key.split("#")
        state = st.get("state", "")
        if state == "MERGED":
            mark = "merged"
        elif state == "CLOSED":
            mark = "closed"
        elif st.get("draft"):
            mark = "draft"
        elif st.get("checks") == "fail":
            mark = "fail"
        elif st.get("checks") == "pending":
            mark = "pending"
        elif st.get("review") == "APPROVED":
            mark = "approved"
        elif state == "OPEN":
            mark = "open"
        else:
            mark = "unknown"
        out.append({"label": f"{repo.split('/')[-1]}#{num}", "mark": mark})
    return out


blocks, cur = {"AGENTS": [], "PS": [], "PANES": []}, None
for line in sys.stdin:
    key = line.strip()
    if key in blocks:
        cur = key
        continue
    if cur:
        blocks[cur].append(line)

try:
    agents = json.loads("".join(blocks["AGENTS"]) or "[]")
except json.JSONDecodeError:
    agents = []
by_pid = {str(a.get("pid")): a for a in agents if a.get("pid")}

parent = {}
for line in blocks["PS"]:
    bits = line.split()
    if len(bits) == 2:
        parent[bits[0]] = bits[1]

pane_pid = {}
for line in blocks["PANES"]:
    bits = line.split()
    if len(bits) == 2:
        pane_pid[bits[0]] = bits[1]


def owner(root):
    """The claude process running under a session's pane, if any."""
    for pid in by_pid:
        cur, hops = pid, 0
        while cur and cur != "1" and hops < 24:
            if cur == root:
                return pid
            cur = parent.get(cur)
            hops += 1
    return None


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


out = []
for name in sorted(os.listdir(REG)):
    if not (name.startswith("cc_") and name.endswith(".json")):
        continue
    rec = load(os.path.join(REG, name))
    if rec is None:
        continue
    tid = name[:-5]
    dirty = False

    root = pane_pid.get(tid)
    cpid = owner(root) if root else None

    # A running conversation is the authority on its own id; --resume forks one.
    if cpid:
        live_sid = by_pid[cpid].get("sessionId")
        if live_sid and live_sid != rec.get("claude_session"):
            rec["claude_session"] = live_sid
            dirty = True

    sid = rec.get("claude_session") or ""
    cached = load(os.path.join(IDX, sid + ".json")) if sid else None
    if cached and cached.get("updated_at", 0) > rec.get("updated_at", 0):
        rec["recap"] = cached.get("recap", "")
        rec["state"] = cached.get("state", "")
        rec["updated_at"] = cached.get("updated_at", 0)
        dirty = True

    state = rec.get("state") or ""
    # A conversation you are still in cannot be finished. The classifier reads
    # the tail of the transcript, where "I fixed it, here is what happened"
    # looks exactly like the end of the thread — so a live, recently active
    # conversation never renders as done however it was labelled.
    if state in ("done", "dead") and root and \
            time.time() - rec.get("updated_at", 0) < 1800:
        state = "pickup"
    if root:
        if not cpid:
            # The session is up but Claude has not registered yet: it is
            # starting, not gone. Calling it dead folded it out of the list for
            # the several seconds a resume takes.
            state = "running"
        elif by_pid[cpid].get("waitingFor") == "dialog open":
            # A modal Claude put up itself — most often the start-from-a-summary
            # offer on resume. It is a keypress you owe it, not a question it
            # asked, so it neither makes a conversation red nor clears the
            # recap's own verdict about one.
            pass
        elif by_pid[cpid].get("waitingFor"):
            state = "answer"
        elif by_pid[cpid].get("status") == "busy":
            state = "running"
    if not state:
        state = "pickup"

    if dirty:
        tmp = os.path.join(REG, tid + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, os.path.join(REG, name))

    out.append({
        "id": tid, "session": tid,
        "topic": rec.get("topic", ""), "label": rec.get("label", ""),
        "recap": " ".join((rec.get("recap") or "").split()),
        # Unread means it finished saying something after you last had it on
        # screen. The Stop hook's updated_at is exactly "it stopped talking",
        # which is a truer signal than transcript mtime — that also ticks while
        # Claude is mid-answer, which would leave every busy row permanently new.
        "unread": rec.get("updated_at", 0) > rec.get("last_seen", 0),
        "prs": pr_badges(tid),
        "state": state, "session_id": sid,
        # A conversation has no updated_at until its first recap is written, and
        # 0 sorts as the oldest thing there is — so a brand-new one fell to the
        # bottom of "most recent first". Fall back to when it was created.
        "updated_at": rec.get("updated_at") or rec.get("created", 0),
        "created": rec.get("created", 0),
        "cold": root is None,
    })

# A declared topic with nothing in it still gets a row, so it holds its place
# in the list the way an empty tab used to. It carries no id: the panel treats
# that as "not a conversation" and only lets you start one here.
if TOPICS:
    declared = load(TOPICS) or []
    used = {r["topic"] for r in out}
    for name in declared:
        if name in used:
            continue
        out.append({
            "id": "", "session": "", "topic": name, "label": "no conversations yet",
            "recap": "", "state": "empty", "session_id": "",
            "updated_at": 0, "created": 0, "cold": True,
            "unread": False, "prs": [],
        })

json.dump(out, sys.stdout)
