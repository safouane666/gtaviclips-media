"""GTA VI Clips cloud scheduler (runs hourly on GitHub Actions; the user's PC can be off).

Keeps Buffer's free-plan queue (max 10 per channel) topped up from queue.json:
  Instagram + YouTube at 17:00 and 21:00, TikTok at 21:00 (local time UTC+1).
  Never the same streamer twice in a row, prefer one not seen in the last 3, alternate male/female.
After a slot has gone out on every network, the video is deleted from the GitHub release.
State lives in state.json and is committed back by the workflow. Log: posting.log
"""
import datetime as dt, json, os, pathlib, random, subprocess, time, urllib.error, urllib.request

HERE = pathlib.Path(__file__).parent
QUEUE, STATE, LOG = HERE / "queue.json", HERE / "state.json", HERE / "posting.log"
API = "https://api.buffer.com"
REPO, TAG = "safouane666/gtaviclips-media", "media"
TZ = dt.timezone(dt.timedelta(hours=1))
CH = {"instagram": "6afde4d0ece6dfde5f0ebac1", "tiktok": "6afe0407ece6dfde5f111eec", "youtube": "6afde355ece6dfde5f0eac8b"}
POST_HOURS, YT_HOURS, TT_HOURS = {17, 21}, {17, 21}, {21}
QUEUE_MAX, MAX_AHEAD = 9, dt.timedelta(days=7)
FINAL = ("sent", "error", "removed")
CREATE = """mutation($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id status } }
  ... on MutationError { message } } }"""
STATUS = "query($id: PostId!) { post(input: {id: $id}) { id status externalLink error { message } } }"


def log(msg):
    line = f"{dt.datetime.now(TZ):%Y-%m-%d %H:%M}  {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")


def gql(key, q, v):
    req = urllib.request.Request(API, data=json.dumps({"query": q, "variables": v}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": f"HTTP {e.code}: {e.read().decode()[:300]}"}]}


def build(svc, r, due):
    video = {"url": r["url"]}
    if r.get("cover_offset_ms") is not None and svc in ("instagram", "tiktok"):
        video["metadata"] = {"thumbnailOffset": int(r["cover_offset_ms"])}
    inp = {"channelId": CH[svc], "text": r["youtube_desc"] if svc == "youtube" else r[svc], "schedulingType": "automatic",
           "mode": "customScheduled", "dueAt": due, "assets": [{"video": video}]}
    if svc == "instagram": inp["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    elif svc == "youtube": inp["metadata"] = {"youtube": {"title": r["youtube_title"][:100], "privacy": "public",
                                                         "categoryId": "20", "madeForKids": False, "notifySubscribers": True}}
    elif svc == "tiktok": inp["metadata"] = {"tiktok": {"title": ""}}
    return inp


def check_posted(key, st, now):
    for rid, b in list(st["booked"].items()):
        if dt.datetime.fromisoformat(b["slot"]) > now: continue
        for pid, info in b["posts"].items():
            if info.get("status") in FINAL: continue
            p = (gql(key, STATUS, {"id": pid}).get("data") or {}).get("post") or {}
            if p.get("status") in ("sent", "error"):
                info.update(status=p["status"], link=p.get("externalLink"), error=(p.get("error") or {}).get("message"))
                log(f"{rid} {info['service']}: {p['status']} {p.get('externalLink') or info.get('error') or ''}")
        if all(i.get("status") in FINAL for i in b["posts"].values()):
            name = b["url"].rsplit("/", 1)[-1]
            subprocess.run(["gh", "release", "delete-asset", TAG, name, "-R", REPO, "-y"], capture_output=True)
            st["done"][rid] = b; del st["booked"][rid]


def pick(queue, st):
    by = {}
    for r in queue.values():
        if r["id"] not in st["booked"] and r["id"] not in st["done"]: by.setdefault(r["streamer"], []).append(r)
    if not by: return None
    recent = st["recent"]
    allowed = [s for s in by if s not in recent[-3:]] or [s for s in by if s not in recent[-1:]] or list(by)
    if recent:
        last_g = st.get("recent_gender", [None])[-1]
        other = [s for s in allowed if by[s][0]["gender"] != last_g]
        if other: allowed = other
    s = random.choices(allowed, weights=[len(by[a]) for a in allowed])[0]
    return random.choice(by[s])


def next_slot(st, now):
    taken = set(st["taken"])
    t = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    while t - now < dt.timedelta(minutes=30) or t.isoformat() in taken or t.hour not in POST_HOURS:
        t += dt.timedelta(hours=1)
    return t


def main():
    key = os.environ["BUFFER_API_KEY"]
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    st = json.loads(STATE.read_text(encoding="utf-8"))
    for k, d in (("taken", []), ("recent", []), ("recent_gender", []), ("booked", {}), ("done", {})): st.setdefault(k, d)
    now = dt.datetime.now(TZ)
    probe = next((pid for b in st["booked"].values() for pid in b["posts"]), None)
    if probe:   # proves the Buffer key works even when there is nothing to book this hour
        ok = ((gql(key, STATUS, {"id": probe}).get("data") or {}).get("post") or {}).get("status")
        print("buffer key check:", "OK" if ok else "FAILED", flush=True)
        if not ok: log("Buffer key check FAILED - is the BUFFER_API_KEY secret still valid?")
    check_posted(key, st, now)
    while True:
        future = sum(1 for b in st["booked"].values()
                     if dt.datetime.fromisoformat(b["slot"]) > now and any(i.get("status") not in FINAL for i in b["posts"].values()))
        slot = next_slot(st, now)
        if future >= QUEUE_MAX or slot - now > MAX_AHEAD: break
        r = pick(queue, st)
        if not r: log("queue empty - nothing left to schedule"); break
        due = slot.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        posts = {}
        for svc in ("instagram", "tiktok", "youtube"):
            if (svc == "youtube" and slot.hour not in YT_HOURS) or (svc == "tiktok" and slot.hour not in TT_HOURS): continue
            for _ in range(3):
                res = (gql(key, CREATE, {"input": build(svc, r, due)}).get("data") or {}).get("createPost") or {}
                if "post" in res or "could not be read" not in str(res.get("message", "")): break
                time.sleep(20)
            if "post" in res: posts[res["post"]["id"]] = {"service": svc, "status": res["post"]["status"]}
            else: log(f"{r['id']} -> {svc} FAILED: {res.get('message') or res}")
        if not posts: break                                     # e.g. Buffer limit: try again next hour
        st["booked"][r["id"]] = {"slot": slot.isoformat(), "url": r["url"], "posts": posts}
        st["taken"] = (st["taken"] + [slot.isoformat()])[-500:]
        st["recent"] = (st["recent"] + [r["streamer"]])[-10:]
        st["recent_gender"] = (st["recent_gender"] + [r["gender"]])[-10:]
        log(f"slot {slot:%a %d %b %H:%M}: {r['id']} ({r['streamer']}) on {', '.join(p['service'] for p in posts.values())}")
    left = sum(1 for r in queue if r not in st["booked"] and r not in st["done"])
    st["status"] = {"checked": now.isoformat(timespec="minutes"), "reels_waiting": left, "booked": len(st["booked"])}
    STATE.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
