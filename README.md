# kuoworks-media

Public media for KUØ WORKS / 廁略 FindToilet social posts (IG + Threads). Images are fetched by the Meta publishing API.

## LinkedIn

`linkedin/posts.json` holds the scheduled LinkedIn posts (English first, Traditional Chinese below) and `linkedin/cards/` their images.
The `LinkedIn posts` GitHub Action runs daily at 13:05 UTC and publishes whatever is due with `linkedin/publish.py`, using the `LINKEDIN_ACCESS_TOKEN` repository secret (60-day token, renew before it expires). Published posts are recorded in `linkedin/posted.json`.
