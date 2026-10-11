#!/usr/bin/env python3
"""Publish the scheduled KUØ WORKS LinkedIn posts in linkedin/posts.json.

Uses only the Python standard library, so it runs as-is on GitHub Actions.

  python3 linkedin/publish.py whoami            check the token, print the author URN
  python3 linkedin/publish.py preview li-01     print a post exactly as it will be sent
  python3 linkedin/publish.py post li-01        publish one post now
  python3 linkedin/publish.py due [--dry-run]   publish every post that is due and not yet posted

Environment:
  LINKEDIN_ACCESS_TOKEN  required; 60-day member token with w_member_social (+ openid, profile)
  LINKEDIN_AUTHOR        optional; e.g. urn:li:organization:123 once Page posting is approved
  LINKEDIN_VERSION       optional; LinkedIn-Version header (YYYYMM), defaults to two months back
  GRACE_HOURS            optional; how late a post may still go out (default 12)
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
ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN = ROOT / "linkedin" / "posts.json"
LOG = ROOT / "linkedin" / "posted.json"

# Characters LinkedIn's "little text" format reserves in post commentary.
RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")
HASHTAG = re.compile(r"(?<![\w&])#(\w+)")


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
        if p["id"] in log:
            continue
        at = dt.datetime.fromisoformat(p["publish_at_utc"].replace("Z", "+00:00"))
        if at <= t < at + grace:
            due.append(p)
        elif t >= at + grace:
            missed.append(p["id"])
    if missed:
        print("skipped (past the grace window, post by hand or move the date): " + ", ".join(missed))
    if not due:
        print("nothing due")
        return
    if dry_run:
        for p in due:
            print(f"would post {p['id']} ({p['publish_at_utc']}) image={p.get('image')}")
        return
    owner = author()
    for p in due:
        publish(p, log, owner)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    for name in ("preview", "post"):
        sub.add_parser(name).add_argument("id")
    d = sub.add_parser("due")
    d.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        if a.cmd == "preview":
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
            if a.id in log:
                raise LinkedInError(f"{a.id} was already posted: {log[a.id]}")
            publish(find(a.id), log, author())
    except LinkedInError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
