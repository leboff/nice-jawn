# Nice Jawn

Posts South Philly's outdoor Nice score (from the [Better Weather](https://github.com/leboff/ha-better-weather)
Home Assistant integration) to Bluesky as [@nice.bof.fish](https://bsky.app/profile/nice.bof.fish).

- **07:00**: today's outlook (score, high/low, rain, best stretch, what's docking it).
- **20:30**: tomorrow's forecast, plus how today's forecast did against the backyard station.

Text is a fixed template over real sensor values; nothing is generated. Each post has a card image with alt text.

```sh
python nice_jawn.py morning --dry-run --out /tmp/nj   # render without posting
python nice_jawn.py serve                              # scheduler (container entrypoint)
```

Env: `HA_URL`, `HA_TOKEN`, `BSKY_HANDLE`, `BSKY_APP_PASSWORD` (an app password, never the account password),
optional `NJ_PLACE`, `NJ_MORNING`, `NJ_EVENING`, `NJ_DRY_RUN=1`. Deployed on Dokploy as a compose stack; secrets live
in the Dokploy env, never in this repo.
