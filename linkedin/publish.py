#!/usr/bin/env python3
"""Publish the scheduled KUØ WORKS LinkedIn posts in linkedin/posts.json.

Uses only the Python standard library, so it runs as-is on GitHub Actions.

Route A, through Buffer (posts to the Company Page and the personal profile):
  python3 linkedin/publish.py buffer-channels             list the connected Buffer channels and their ids
  python3 linkedin/publish.py buffer-schedule [--dry-run] queue every future post in Buffer at its time
  python3 linkedin/publish.py buffer-unschedule [--channel ID ...] [--dry-run]
                                                          delete the still-queued posts (all channels, or only the given ones)

Route B, straight to the LinkedIn API (personal profile only):
  python3 linkedin/publish.py whoami            check the token, print the author URN
  python3 linkedin/publish.py preview li-01     print a post exactly as LinkedIn will receive it
  python3 linkedin/publish.py post li-01        publish one post now
  python3 linkedin/publish.py due [--dry-run]   publish every post that is due and not yet posted

Environment:
  BUFFER_API_KEY         route A; personal key from publish.buffer.com/settings/api
  BUFFER_CHANNEL_IDS     route A, optional; comma-separated ids, default every LinkedIn channel
  MEDIA_BASE_URL         route A, optional; public base URL for images (default: this repo's main branch)
  LINKEDIN_ACCESS_TOKEN  route B; 60-day member token with w_member_social (+ openid, profile)
  LINKEDIN_AUTHOR        route B, optional; e.g. urn:li:organization:123 once Page posting is approved
  LINKEDIN_VERSION       route B, optional; LinkedIn-Version header (YYYYMM), defaults to two months back
  GRACE_HOURS            route B, optional; how late a post may still go out (default 12)
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("LINKEDIN_API_BASE", "https://api.linkedin.com")
BUFFER_API = os.environ.get("BUFFER_API_BASE", "https://api.buffer.com")
MEDIA_BASE = os.environ.get("MEDIA_BASE_URL", "https://raw.githubusercontent.com/jaykuo0619/kuoworks-media/main/")
ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN = ROOT / "linkedin" / "posts.json"
LOG = ROOT / "linkedin" / "posted.json"

# Characters LinkedIn's "little text" format reserves in post commentary.
RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")
HASHTAG = re.compile(r"(?<![\w&/])#([^\W\d_]\w*)")


class LinkedInError(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc)


def api_version():
    if os.environ.get("LINKEDIN_VERSION"):
        return os.environ["LINKEDIN_VERSION"]
    d = now().date().replace(day=1)
    for _ in range(2):
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return d.strftime("%Y%m")


def token():
    t = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
    if not t:
        raise LinkedInError("LINKEDIN_ACCESS_TOKEN is not set")
    return t


def call(method, url, body=None, raw=None, rest=True, extra=None):
    headers = {"Authorization": f"Bearer {token()}"}
    if rest:
        headers["LinkedIn-Version"] = api_version()
        headers["X-Restli-Protocol-Version"] = "2.0.0"
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    headers.update(extra or {})
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode() or "{}"
            return r.status, dict(r.headers), json.loads(text) if text.lstrip().startswith("{") else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        hint = ""
        if e.code == 401:
            hint = " (token expired or revoked: generate a new one and update the LINKEDIN_ACCESS_TOKEN secret)"
        elif e.code == 426 or "VERSION" in detail.upper():
            hint = f" (LinkedIn-Version {api_version()} rejected: set LINKEDIN_VERSION to a current YYYYMM)"
        raise LinkedInError(f"{method} {url} -> HTTP {e.code}{hint}\n{detail}") from None


def little_text(text):
    """Escape reserved characters and turn #word into real LinkedIn hashtags."""
    tags = []

    def keep(m):
        tags.append(m.group(1))
        return f"\x00{len(tags) - 1}\x00"

    text = HASHTAG.sub(keep, text)
    text = RESERVED.sub(r"\\\1", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: "{hashtag|\\#|" + tags[int(m.group(1))] + "}", text)


def author():
    if os.environ.get("LINKEDIN_AUTHOR"):
        return os.environ["LINKEDIN_AUTHOR"]
    _, _, me = call("GET", f"{API}/v2/userinfo", rest=False)
    if "sub" not in me:
        raise LinkedInError(f"/v2/userinfo returned no 'sub' (token needs the openid scope): {me}")
    return f"urn:li:person:{me['sub']}"


def upload_image(owner, path):
    _, _, init = call("POST", f"{API}/rest/images?action=initializeUpload",
                      {"initializeUploadRequest": {"owner": owner}})
    value = init["value"]
    call("PUT", value["uploadUrl"], raw=path.read_bytes(), rest=False,
         extra={"Content-Type": "application/octet-stream"})
    urn = value["image"]
    for _ in range(30):  # wait until LinkedIn has processed the upload
        _, _, info = call("GET", f"{API}/rest/images/{urllib.parse.quote(urn, safe='')}")
        if info.get("status") == "AVAILABLE":
            break
        time.sleep(2)
    return urn


def create_post(owner, post):
    body = {
        "author": owner,
        "commentary": little_text(post["text"]),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if post.get("image"):
        urn = upload_image(owner, ROOT / post["image"])
        body["content"] = {"media": {"id": urn, "altText": post.get("alt", "")[:4086]}}
    _, headers, _ = call("POST", f"{API}/rest/posts", body)
    return headers.get("x-restli-id") or headers.get("X-RestLi-Id") or "?"


def load_plan():
    return json.loads(PLAN.read_text(encoding="utf-8"))


def load_log():
    return json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else {}


def save_log(log):
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find(post_id):
    for p in load_plan():
        if p["id"] == post_id:
            return p
    raise LinkedInError(f"no post with id {post_id} in {PLAN.name}")


def publish(post, log, owner):
    urn = create_post(owner, post)
    log[post["id"]] = {"posted_at": now().isoformat(timespec="seconds"), "urn": urn}
    save_log(log)
    print(f"posted {post['id']} -> https://www.linkedin.com/feed/update/{urn}/")


def cmd_due(dry_run):
    grace = dt.timedelta(hours=float(os.environ.get("GRACE_HOURS", "12")))
    log, t = load_log(), now()
    due, missed = [], []
    for p in load_plan():
        if p["id"] in log or any(k.startswith(p["id"] + "@buffer:") for k in log):
            continue  # already posted, or already queued in Buffer
        at = dt.datetime.fromisoformat(p["publish_at_utc"].replace("Z", "+00:00"))
        if at <= t < at + grace:
            due.append(p)
        elif t >= at + grace:
            missed.append(p["id"])
    if missed:
        print("skipped (past the grace window, post by hand or move the date): " + ", ".join(missed))
    if not due:
        print("nothing due")
        if missed:
            sys.exit(2)
        return
    if dry_run:
        for p in due:
            print(f"would post {p['id']} ({p['publish_at_utc']}) image={p.get('image')}")
        return
    owner = author()
    for p in due:
        publish(p, log, owner)


# ---------------------------------------------------------------- Buffer (route A)

def buffer_gql(query, variables=None):
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    if not key:
        raise LinkedInError("BUFFER_API_KEY is not set")
    req = urllib.request.Request(
        BUFFER_API, method="POST",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "kuoworks-media-linkedin/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise LinkedInError(f"Buffer API -> HTTP {e.code}\n{e.read().decode(errors='replace')[:800]}") from None
    if out.get("errors"):
        raise LinkedInError("Buffer API error: " + json.dumps(out["errors"], ensure_ascii=False)[:800])
    return out["data"]


def buffer_channels():
    data = buffer_gql("query { account { organizations { id name } } }")
    channels = []
    for org in data["account"]["organizations"]:
        q = "query { channels(input: {organizationId: %s}) { id name displayName service } }" % json.dumps(org["id"])
        for c in buffer_gql(q)["channels"]:
            channels.append(c)
    return channels


def buffer_targets():
    if os.environ.get("BUFFER_CHANNEL_IDS"):
        return [c.strip() for c in os.environ["BUFFER_CHANNEL_IDS"].split(",") if c.strip()]
    ids = [c["id"] for c in buffer_channels() if "linkedin" in (c.get("service") or "").lower()]
    if not ids:
        raise LinkedInError("no LinkedIn channel connected in Buffer (connect the Page and profile first)")
    return ids


CREATE_POST = """mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}"""


def cmd_buffer_schedule(dry_run):
    log, soon = load_log(), now() + dt.timedelta(minutes=15)
    todo = [p for p in load_plan()
            if dt.datetime.fromisoformat(p["publish_at_utc"].replace("Z", "+00:00")) > soon]
    channels = buffer_targets() if not dry_run or os.environ.get("BUFFER_API_KEY") else ["<channel>"]
    for p in todo:
        for ch in channels:
            key = f"{p['id']}@buffer:{ch}"
            if key in log or p["id"] in log:
                continue
            due = p["publish_at_utc"].replace("Z", ".000Z")
            if dry_run:
                print(f"would queue {p['id']} on {ch} for {due}")
                continue
            post = {"text": p["text"], "channelId": ch, "schedulingType": "automatic",
                    "mode": "customScheduled", "dueAt": due}
            if p.get("image"):
                post["assets"] = [{"image": {"url": MEDIA_BASE + p["image"]}}]
            res = buffer_gql(CREATE_POST, {"input": post})["createPost"]
            if "message" in res and "post" not in res:
                raise LinkedInError(f"Buffer refused {p['id']} on {ch}: {res['message']}")
            log[key] = {"queued_at": now().isoformat(timespec="seconds"), "buffer_post": res["post"]["id"], "due": due}
            save_log(log)
            print(f"queued {p['id']} on {ch} for {due}")


DELETE_POST = """mutation($input: DeletePostInput!) {
  deletePost(input: $input) {
    ... on DeletePostSuccess { id }
    ... on VoidMutationError { message }
  }
}"""


def cmd_buffer_unschedule(channels, dry_run):
    """Delete queued posts that have not gone out yet. Entries stay in posted.json, marked
    deleted_at, so buffer-schedule does not queue them again; remove an entry to resume it."""
    log, cutoff = load_log(), now() + dt.timedelta(minutes=5)
    for key, rec in sorted(log.items(), key=lambda kv: kv[1].get("due", "")):
        if "@buffer:" not in key or rec.get("deleted_at") or not rec.get("buffer_post"):
            continue
        if channels and key.split("@buffer:", 1)[1] not in channels:
            continue
        if dt.datetime.fromisoformat(rec["due"].replace("Z", "+00:00")) <= cutoff:
            print(f"left alone {key}: due {rec['due']} (already published or about to be)")
            continue
        if dry_run:
            print(f"would delete {key} ({rec['buffer_post']}) due {rec['due']}")
            continue
        res = buffer_gql(DELETE_POST, {"input": {"id": rec["buffer_post"]}})["deletePost"]
        if "message" in res:
            raise LinkedInError(f"Buffer would not delete {key}: {res['message']}")
        rec["deleted_at"] = now().isoformat(timespec="seconds")
        save_log(log)
        print(f"deleted {key} due {rec['due']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    sub.add_parser("buffer-channels")
    b = sub.add_parser("buffer-schedule")
    b.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("buffer-unschedule")
    u.add_argument("--channel", action="append", default=[])
    u.add_argument("--dry-run", action="store_true")
    for name in ("preview", "post"):
        sub.add_parser(name).add_argument("id")
    d = sub.add_parser("due")
    d.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        if a.cmd == "buffer-channels":
            for c in buffer_channels():
                print(f"{c['id']}  {c.get('service')}  {c.get('displayName') or c.get('name')}")
        elif a.cmd == "buffer-schedule":
            cmd_buffer_schedule(a.dry_run)
        elif a.cmd == "buffer-unschedule":
            cmd_buffer_unschedule(a.channel, a.dry_run)
        elif a.cmd == "preview":
            print(little_text(find(a.id)["text"]))
        elif a.cmd == "due" and a.dry_run:
            cmd_due(True)
        elif a.cmd == "due" and not os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip():
            print("LINKEDIN_ACCESS_TOKEN is not set yet; nothing posted.")
        elif a.cmd == "due":
            cmd_due(False)
        elif a.cmd == "whoami":
            print(author(), f"(LinkedIn-Version {api_version()})")
        elif a.cmd == "post":
            log = load_log()
            if a.id in log or any(k.startswith(a.id + "@buffer:") for k in log):
                raise LinkedInError(f"{a.id} was already posted: {log[a.id]}")
            publish(find(a.id), log, author())
    except LinkedInError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
