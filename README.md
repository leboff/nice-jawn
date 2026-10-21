# Nice Jawn

Posts South Philly's outdoor Nice score (from the [Better Weather](https://github.com/leboff/ha-better-weather)
Home Assistant integration) to Bluesky as [@nice.bof.fish](https://bsky.app/profile/nice.bof.fish).

- **07:00**: today's outlook (score, high/low, rain, best stretch, what's docking it).
- **20:30**: recap of today: measured score, all-time rank, the day just above/below it, how last night's forecast did.
  The card shows where today falls in every measured day (distribution + the five days ranked around it).
  Waits up to 20 min for Better Weather to rank today; after that it posts without the rank.
- **20:31**: tomorrow's forecast.

- **On demand:** mention it ("hey @nice.bof.fish how are we looking") and it replies in-thread with the live score and a
  card of today's hours (solid = measured, faded = forecast). Say "tomorrow" to get tomorrow's forecast instead.
  2-minute cooldown per person, 20 replies/hour cap.

Text is a fixed template over real sensor values; nothing is generated. Each post has a card image with alt text.

```sh
python nice_jawn.py morning --dry-run --out /tmp/nj   # render without posting
python nice_jawn.py serve                              # scheduler (container entrypoint)
```

Env: `HA_URL`, `HA_TOKEN`, `BSKY_HANDLE`, `BSKY_APP_PASSWORD` (an app password, never the account password),
optional `NJ_PLACE`, `NJ_MORNING`, `NJ_EVENING`, `NJ_DRY_RUN=1`, `NJ_REPLIES=live|dry|off`. Deployed on Dokploy as a compose stack; secrets live
in the Dokploy env, never in this repo.
