"""What each conversation has touched: pull requests, tokens, and time.

Deterministic end to end. PR references are harvested from the transcripts by
pattern, never inferred, and their state comes from `gh`. Token and time totals
come from the usage records Claude Code already writes. One incremental pass
over each transcript feeds both, so a tick costs almost nothing once the first
scan is done.

  cc-harvest.py harvest <reg> <proj>    scan transcripts
  cc-harvest.py status                  refresh PR state from GitHub
"""
import json, os, re, subprocess, sys, time

HOME = os.path.expanduser("~")
COCKPIT = os.path.join(HOME, ".claude", "cockpit")
REFS = os.path.join(COCKPIT, "prs.json")
USE = os.path.join(COCKPIT, "usage.json")
STATE = os.path.join(COCKPIT, "pr-status.json")

PR_URL = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
# A conversation's own PR is the one it ran a command against. Scraping every
# URL in the transcript surfaced whatever a `pr list` happened to print — a
# conversation asking *about* other PRs showed those instead of its own.
# An invocation, not a mention: `gh pr <sub>` has to start a command segment.
# Matching a bare `pr view` anywhere in the command string credited this very
# conversation with a PR, because a grep PATTERN containing "gh pr create"
# looked identical to running it.
PR_CMD = re.compile(r"(?:^|[;&|\n(])\s*gh\s+pr\s+"
                    r"(view|diff|checkout|edit|ready|merge|comment|review|"
                    r"close|reopen|create)\b")
PR_REPO = re.compile(r"--repo[= ]\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
# Flags that swallow the value after them, so their argument is never mistaken
# for the PR number.
VALUED = {"--repo", "-R", "--json", "--jq", "-q", "--template", "-t", "--body",
          "-b", "--title", "-B", "--base", "--head", "-H", "--limit", "-L"}
# Acting on a PR outranks reading one, and the conversation's own label — often
# just the number — outranks both.
PR_WEIGHT = {"create": 4, "merge": 3, "ready": 3, "edit": 3, "comment": 3,
             "review": 3, "close": 3, "reopen": 3, "checkout": 3, "diff": 2,
             "view": 1}
PR_FLOOR = 2      # below this it was only mentioned, not worked on
HARVEST_EVERY = 10      # seconds; the transcripts barely move between ticks
STATUS_EVERY = 120      # seconds; one gh call per repo, so keep it unhurried
# The first sight of a transcript is read in full. Truncating it would make the
# token and time totals silently wrong, and reporting those to work is the point.
MAX_TAIL = 0


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def save(path, data):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


BASE_DIRS = ()      # filled per run from the cockpit directory
RECENT_CWDS = 60    # how much of the tail counts double when ranking repos
IDLE_GAP = 600      # a pause longer than this is you being elsewhere, not work


def new_usage():
    return {"models": {}, "seen": [], "active": 0, "first": "", "last": "",
            "turns": 0}


def prime(u):
    """`seen` round-trips through JSON as a list; the hot path wants a set."""
    u["seen_set"] = set(u.get("seen") or [])
    return u


def note_pr(entry, key, weight, now):
    prs = entry.setdefault("prs", {})
    cur = prs.get(key)
    if not isinstance(cur, dict):
        cur = {"w": 0, "at": 0}
    cur["w"] = max(cur["w"], weight)
    cur["at"] = now
    prs[key] = cur


def pr_number(rest):
    """The first bare integer that is an argument, not a flag's value."""
    toks = rest.split()
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if t in VALUED:
            skip = True
            continue
        if t.startswith("-"):
            continue
        if t.isdigit() and len(t) <= 7:
            return t
        return None            # a non-flag, non-number argument ends the args
    return None


def git_root(path, base):
    """Nearest enclosing checkout, stopping at the cockpit's own directory."""
    p = path
    while p and p != "/" and p != base:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        p = os.path.dirname(p)
    return None


def note_cwd(entry, cwd, base):
    """Where a conversation works, ranked later by how often it was there.

    The directory it was started in says nothing - every conversation starts in
    the same place. Where it SETTLED is the useful signal, and that has to be
    counted rather than taken from the last line: conversations wander through
    dozens of directories, so the final one is wherever the last command
    happened to run.
    """
    root = git_root(cwd, base)
    if not root:
        return
    counts = entry.setdefault("cwd_counts", {})
    counts[root] = counts.get(root, 0) + 1
    tail = entry.setdefault("cwd_tail", [])
    tail.append(root)
    if len(tail) > RECENT_CWDS:
        del tail[:-RECENT_CWDS]


def scan_commands(entry, d, now, default_repo):
    """PRs this conversation actually ran a command against."""
    for c in (d.get("message") or {}).get("content") or []:
        if not (isinstance(c, dict) and c.get("type") == "tool_use"):
            continue
        cmd = (c.get("input") or {}).get("command") or ""
        if "gh" not in cmd or " pr " not in cmd:
            continue
        for m in PR_CMD.finditer(cmd):
            sub = m.group(1)
            rest = cmd[m.end():m.end() + 160].split("\n")[0]
            repo_m = PR_REPO.search(rest)
            repo = repo_m.group(1) if repo_m else default_repo
            if not repo:
                continue
            num = pr_number(rest)
            if num:
                note_pr(entry, f"{repo}#{num}", PR_WEIGHT.get(sub, 1), now)
            elif sub == "create":
                # The number only exists once GitHub answers, so remember the
                # call and read it out of that call's own result.
                entry.setdefault("await_create", {})[c.get("id") or ""] = repo


def claim_created(entry, d, now):
    """A PR this conversation created: the URL comes back in that call's result."""
    awaiting = entry.get("await_create") or {}
    if not awaiting:
        return
    for c in (d.get("message") or {}).get("content") or []:
        if not (isinstance(c, dict) and c.get("type") == "tool_result"):
            continue
        repo = awaiting.pop(c.get("tool_use_id") or "", "")
        if not repo:
            continue
        text = json.dumps(c.get("content"))
        for r, num in PR_URL.findall(text):
            if r == repo:
                note_pr(entry, f"{r}#{num}", PR_WEIGHT["create"], now)
                break


def scan_chunk(entry, u, chunk, now, label, base):
    """One parse of the new bytes feeds PR attribution and usage together."""
    repos = [m.group(1) for m in PR_URL.finditer(chunk.decode("utf-8", "ignore"))]
    default_repo = entry.get("repo") or (max(set(repos), key=repos.count) if repos else "")
    if default_repo:
        entry["repo"] = default_repo
    for raw in chunk.split(b"\n"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") == "user":
            claim_created(entry, d, now)
            continue
        cwd = d.get("cwd")
        if cwd:
            note_cwd(entry, cwd, base)
        if d.get("type") != "assistant":
            continue
        scan_commands(entry, d, now, default_repo)
        count_usage_line(u, d)
    lab = re.findall(r"\d{2,6}", label or "")
    for key in list((entry.get("prs") or {}).keys()):
        if key.split("#")[1] in lab:
            entry["prs"][key]["w"] = max(entry["prs"][key]["w"], 9)


def count_usage_line(u, d):
    """Tokens per model plus active time, deduped by (message.id, requestId).

    Claude Code writes the same usage record more than once per message, so
    counting lines would inflate every total.
    """
    if d.get("type") != "assistant":
        return
    msg = d.get("message") or {}
    use = msg.get("usage") or {}
    if not use:
        return
    key = f"{msg.get('id')}:{d.get('requestId')}"
    if key in u["seen_set"]:
        return
    u["seen_set"].add(key)
    u["seen"].append(key)
    u["turns"] += 1
    m = u["models"].setdefault(msg.get("model") or "unknown",
                               {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0})
    m["in"] += use.get("input_tokens", 0)
    m["out"] += use.get("output_tokens", 0)
    m["cache_write"] += use.get("cache_creation_input_tokens", 0)
    m["cache_read"] += use.get("cache_read_input_tokens", 0)
    ts = d.get("timestamp") or ""
    if not ts:
        return
    if not u["first"]:
        u["first"] = ts
    prev = u.get("last") or ""
    if prev:
        gap = iso_delta(prev, ts)
        if 0 < gap <= IDLE_GAP:
            u["active"] += gap
    u["last"] = ts
    # A bounded memory of what has been counted; older ids cannot recur, because
    # the byte offset never goes back over them.
    if len(u["seen"]) > 500:
        u["seen"] = u["seen"][-500:]
        u["seen_set"] = set(u["seen"])


def iso_delta(a, b):
    try:
        import datetime
        fa = datetime.datetime.fromisoformat(a.replace("Z", "+00:00"))
        fb = datetime.datetime.fromisoformat(b.replace("Z", "+00:00"))
        return (fb - fa).total_seconds()
    except Exception:
        return 0


def harvest(reg_dir, proj_dir, base=""):
    refs = load(REFS, {})
    usage = load(USE, {})
    now = int(time.time())
    if now - refs.get("_harvested_at", 0) < HARVEST_EVERY:
        return refs
    for name in os.listdir(reg_dir):
        if not (name.startswith("cc_") and name.endswith(".json")):
            continue
        tid = name[:-5]
        rec = load(os.path.join(reg_dir, name), None)
        if not rec:
            continue
        sid = rec.get("claude_session") or ""
        # Each conversation carries the directory it runs in, so its transcript
        # sits under that path's project folder, not one global one.
        cwd = rec.get("cwd") or ""
        rec_proj = os.path.join(
            HOME, ".claude", "projects", cwd.replace("/", "-")) if cwd else proj_dir
        path = os.path.join(rec_proj, sid + ".jsonl") if sid else ""
        if not path or not os.path.exists(path):
            continue
        entry = refs.setdefault(tid, {"offsets": {}, "prs": {}})
        start = entry["offsets"].get(sid, 0)
        size = os.path.getsize(path)
        if size < start:            # the file was replaced; read it again
            start = 0
        if size == start:
            continue
        # A first sighting of a long transcript reads only its tail: the whole
        # file can be tens of megabytes and the recent PRs are what matter.
        if MAX_TAIL and start == 0 and size > MAX_TAIL:
            start = size - MAX_TAIL
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read()
        entry["offsets"][sid] = size
        scan_chunk(entry, prime(usage.setdefault(tid, new_usage())), chunk, now,
                   rec.get("label", ""), base)
    for u in usage.values():
        u.pop("seen_set", None)
    refs["_harvested_at"] = now
    save(REFS, refs)
    save(USE, usage)
    return refs


def rollup(pr):
    checks = pr.get("statusCheckRollup") or []
    worst = "pass"
    for c in checks:
        v = (c.get("conclusion") or c.get("state") or "").upper()
        if v in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            return "fail"
        if v in ("PENDING", "IN_PROGRESS", "QUEUED", "WAITING", "EXPECTED", ""):
            worst = "pending"
    return worst if checks else "none"


def status(repos, wanted=()):
    st = load(STATE, {})
    now = int(time.time())
    if now - st.get("_fetched_at", 0) < STATUS_EVERY:
        return st
    out = {"_fetched_at": now}
    for repo in repos:
        try:
            raw = subprocess.run(
                ["gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "100",
                 "--json", "number,isDraft,state,statusCheckRollup,reviewDecision"],
                capture_output=True, text=True, timeout=25).stdout
            for pr in json.loads(raw or "[]"):
                out[f"{repo}#{pr['number']}"] = {
                    "state": pr.get("state", ""),          # OPEN / MERGED / CLOSED
                    "draft": bool(pr.get("isDraft")),
                    "checks": rollup(pr),
                    "review": pr.get("reviewDecision") or "",
                }
        except Exception:
            # Keep whatever we knew about this repo rather than blanking it.
            for k, v in st.items():
                if k.startswith(repo + "#"):
                    out[k] = v
    # An older PR falls outside the recent-100 listing, so ask about the few
    # that are still unaccounted for by name rather than leaving them blank.
    missing = [k for k in wanted if k not in out][:10]
    for key in missing:
        repo, num = key.split("#")
        try:
            raw = subprocess.run(
                ["gh", "pr", "view", num, "--repo", repo,
                 "--json", "number,isDraft,state,statusCheckRollup,reviewDecision"],
                capture_output=True, text=True, timeout=20).stdout
            pr = json.loads(raw or "{}")
            if pr:
                out[key] = {"state": pr.get("state", ""), "draft": bool(pr.get("isDraft")),
                            "checks": rollup(pr), "review": pr.get("reviewDecision") or ""}
        except Exception:
            pass
    save(STATE, out)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "harvest":
        harvest(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
    elif cmd == "status":
        refs = load(REFS, {})
        wanted = sorted({k for t, e in refs.items()
                         if isinstance(e, dict) for k in (e.get("prs") or {})})
        repos = sorted({k.split("#")[0] for k in wanted})
        status(repos, wanted)
    else:
        sys.exit("usage: cc-harvest.py harvest <reg> <proj> | status")
