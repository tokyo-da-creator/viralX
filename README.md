# PokeRadar

Pushes Pokémon TCG stories to your phone **before** Dexerto, Checkpoint and the big X accounts post them.

Every few minutes it checks Google News (English + Japanese), Bing News, Reddit and PokeBeach, scores each new story against the viral filter, and only pings you when one is worth posting.

## What the alerts mean

| Alert | Meaning |
|---|---|
| **EARLY: big accounts don't have this yet** | Came from a local outlet, Reddit, a court/DOJ release, etc. Post it. |
| **Already on Dexerto (etc.)** | You're second wave. Only post if you can package it better. |
| **Spreading: …** | A story you were alerted on is now on 3+ outlets. It's going big. |

Scores: villain words (+3), chaos/lines/fights (+2), scoops/leaks (+2), human angle (+1), $10K+ (+2), $1M+ (+3), upstream source (+2), under 2h old (+1). Off-topic (Pokémon GO, guides, shopping deals) −5. Default alert threshold: **5**.

## Setup (10 minutes, free)

### 1. Phone alerts
1. Install the **ntfy** app (iOS / Android).
2. Tap **+** and subscribe to a topic with a long random name nobody can guess, e.g. `pokeradar-` + 12 random characters. Anyone who knows the name can read it, so treat it like a password.

### 2. Put it on GitHub
1. Create a new **public** repo (public repos get unlimited free Actions minutes; private ones would run out at this frequency). Upload everything in this folder, including the `.github` folder.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - `NTFY_TOPIC` = your topic name
   - `DISCORD_WEBHOOK_URL` = optional, if you also want alerts in a Discord channel
3. **Actions** tab → enable workflows → **PokeRadar → Run workflow**.

You'll get a "PokeRadar is live" ping. The first run only memorises what's already out there; real alerts start from the next run.

## Run it on your own computer instead (faster)

GitHub schedules can lag a few minutes, and Reddit sometimes blocks cloud servers. On your own machine you can scan every 2 minutes:

```bash
pip install -r requirements.txt
export NTFY_TOPIC=your-topic-name
python radar.py --test-notify   # check your phone buzzes
python radar.py --loop 120      # scan every 2 minutes
```

## Tuning

Everything lives in `config.json`, so no code changes are needed:
- **Too many alerts?** Raise `min_score` to 6.
- **Missing stuff?** Add search queries under `google_news` or `bing_news`, or add words to the `villain` / `chaos` lists.
- **Add a source**: any RSS feed goes in `rss`.
- **New aggregator stealing your stories?** Add its domain to `aggregators` so the radar flags it as second wave.

`python radar.py --dry-run` prints every current story with its score and reasons, without sending anything.

## Notes
- GitHub pauses scheduled workflows in public repos after 60 days with no commits. Push any small change to wake it up.
- PokeBeach blocks some servers. If its feed fails in the logs, follow @PokeBeach on X with notifications on instead.
- X, TikTok and Instagram have no free feeds, so keep your X Pro search columns for those.
