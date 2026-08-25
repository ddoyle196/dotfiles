import json, os, sys

reg, path, fmt = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
try:
    use = json.load(open(path))
except Exception:
    use = {}

rows = []
for name in sorted(os.listdir(reg)):
    if not (name.startswith("cc_") and name.endswith(".json")):
        continue
    tid = name[:-5]
    try:
        rec = json.load(open(os.path.join(reg, name)))
    except Exception:
        continue
    u = use.get(tid) or {}
    models = u.get("models") or {}
    tot = {k: sum(m.get(k, 0) for m in models.values())
           for k in ("in", "out", "cache_write", "cache_read")}
    rows.append({
        "topic": rec.get("topic", ""), "label": rec.get("label", ""),
        "turns": u.get("turns", 0), "active": u.get("active", 0),
        "models": sorted(models), **tot,
    })

if fmt == "csv":
    print("topic,conversation,turns,active_minutes,input,output,cache_write,cache_read,models")
    for r in sorted(rows, key=lambda r: (r["topic"], r["label"])):
        print(f'{r["topic"]},"{r["label"]}",{r["turns"]},{r["active"]/60:.1f},'
              f'{r["in"]},{r["out"]},{r["cache_write"]},{r["cache_read"]},'
              f'"{" ".join(r["models"])}"')
    raise SystemExit

def hm(sec):
    h, m = divmod(int(sec) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"

def k(n):
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else f"{n/1000:.0f}k"

topics = {}
for r in rows:
    topics.setdefault(r["topic"], []).append(r)

gt = {x: 0 for x in ("turns", "active", "in", "out", "cache_write", "cache_read")}
print(f'  {"conversation":40} {"turns":>6} {"active":>8} {"in":>8} {"out":>8} {"cache":>9}')
for topic in sorted(topics):
    rs = topics[topic]
    sub = {x: sum(r[x] for r in rs) for x in gt}
    print(f'\n  {topic}')
    for r in sorted(rs, key=lambda r: -r["active"]):
        print(f'    {r["label"][:38]:38} {r["turns"]:>6} {hm(r["active"]):>8} '
              f'{k(r["in"]):>8} {k(r["out"]):>8} {k(r["cache_write"]+r["cache_read"]):>9}')
    print(f'    {"- topic total":38} {sub["turns"]:>6} {hm(sub["active"]):>8} '
          f'{k(sub["in"]):>8} {k(sub["out"]):>8} {k(sub["cache_write"]+sub["cache_read"]):>9}')
    for x in gt:
        gt[x] += sub[x]
print(f'\n  {"ALL":40} {gt["turns"]:>6} {hm(gt["active"]):>8} '
      f'{k(gt["in"]):>8} {k(gt["out"]):>8} {k(gt["cache_write"]+gt["cache_read"]):>9}')
