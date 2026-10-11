# kuoworks-media

Public media for KUØ WORKS / 廁略 FindToilet social posts (IG + Threads). Images are fetched by the Meta publishing API.

## LinkedIn

`linkedin/posts.json` holds the scheduled LinkedIn posts (English first, Traditional Chinese below) and `linkedin/cards/` their images; `linkedin/page/` has the Page logo and cover.

- Route A (recommended): run the `LinkedIn posts` workflow by hand with task `buffer-schedule`. It queues every future post in Buffer (repository secret `BUFFER_API_KEY`), which publishes them to the KUØ WORKS Page and Jay's profile at their times. Images are fetched from this repo's `main` branch.
- Route B: with a `LINKEDIN_ACCESS_TOKEN` secret (60-day token), the workflow's daily 13:05 UTC run posts due items straight to Jay's profile through the LinkedIn API.

Use one route, not both. Queued and published posts are recorded in `linkedin/posted.json`.
