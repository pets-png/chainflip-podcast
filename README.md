# Cross-Chain Daily (Chainflip podcast)

Turns published posts from the [Chainflip blog](https://chainflip.io/blog) into a daily,
conversational podcast with a recurring cast, voiced with [Fish Audio](https://fish.audio),
and distributed to Spotify (and any other podcast app) through an RSS feed.

Sibling repo to the blog writer: [`pets-png/blog_sheeeeeesh_writer`](https://github.com/pets-png/blog_sheeeeeesh_writer).
The two are linked through the live blog, not through code: the blog writer drafts posts into
Framer, a human publishes them, and this pipeline only ever reads what is live on chainflip.io.

- **Feed:** https://pets-png.github.io/chainflip-podcast/feed.xml
- **Site:** https://pets-png.github.io/chainflip-podcast/

## How a day works

`.github/workflows/daily.yml` runs at 06:00 UTC:

1. **Publish what cleared review.** Pending episodes older than 24h go live unless vetoed: the
   draft GitHub Release becomes public, the episode is added to `docs/feed.xml`, and Spotify
   picks it up on its next crawl. If the source post has been unpublished, or the blog put out
   a post on the same topic in the last 3 days, the episode is dropped or held.
2. **Prepare the next one.**
   - `catalog`: sync the live sitemap and classify new posts into topic clusters.
   - `picker`: choose a back-catalog post (at least 14 days old, no repeat of a cluster in the
     first 50 episodes, then a 60-episode cooldown, with a Claude overlap check on top).
   - `scriptwriter`: Claude writes the conversation.
   - `voices`: Fish Audio `drama-3` renders it scene by scene, and ffmpeg mixes and
     loudness-normalises it to -16 LUFS.
   - The MP3 lands in a draft release, a review issue opens, and Discord gets the audio.

### Reviewing an episode

Each episode gets an issue labelled `episode-pending` with the full script and a link to the audio.

- Do nothing: it publishes after 24h.
- Comment `/approve`: it publishes on the next run.
- Comment `/veto`, add the `veto` label, or close the issue: it's dropped and its post is never reused.

## Why unpublished drafts never become episodes

The blog writer creates Framer **drafts**. Only posts that are actually live show up in
`chainflip.io/sitemap.xml` and return 200, and the catalog only ever reads from there. A post
must also have been live for 14 days, and the live check runs again right before publishing.

## Formats

| Format | Share | Length | When |
|---|---|---|---|
| Hosts + topic guest | ~70% | 6-9 min | Whenever the topic has a regular guest |
| Hosts only | ~30% | 4-6 min | Balances the mix |
| Deep dive | occasional | 12-18 min | Complex posts only (complexity ≥4), every 14-21 episodes |

The cast lives in `cast.yaml`: hosts Jules and Nadia, plus recurring guests per beat (Marco for
lending, Hank for Bitcoin, Kai for Solana, Elena for Ethereum, Mei for stablecoins/Tron, Oscar for
FLIP and protocol, Rook for security, Priya for liquidity, Sam for integrations, Ivy for
regulation). Each episode adds small character details to `data/lore.json`, and later
episodes can call back to them.

## Setup

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt
```

Secrets (repo Settings → Secrets → Actions): `ANTHROPIC_API_KEY`, `FISH_API_KEY`,
`DISCORD_WEBHOOK`, `DISCORD_USER_ID`.

### Casting voices (once)

Each character needs a Fish voice. Audition original voices designed from the character's
`voice_brief`, listen, and lock one in:

```bash
python run.py cast jules            # writes casting/jules/candidate_0..3.wav
python run.py cast jules --pick 2   # creates a private Fish voice model, saves voice_id to cast.yaml
```

You can also paste any Fish library voice ID into `cast.yaml`.

### Useful commands

```bash
python run.py plan --n 50           # preview the next 50 picks without generating anything
python run.py generate --dry-run    # full episode locally (script + audio), no release/issue/Discord
python run.py generate --count 3    # launch: prepare 3 episodes at once
python run.py cover                 # rebuild docs/cover.jpg (needs the Aeonik fonts locally)
```

## Spotify (one-time)

1. Enable GitHub Pages: Settings → Pages → Deploy from branch → `main` / `/docs`.
2. Publish at least one episode so the feed has an item.
3. Go to [creators.spotify.com](https://creators.spotify.com) and sign in with a shared Chainflip
   account. Choose **Find an existing show** (or "I already have a podcast") and paste the feed URL.
4. Spotify emails a code to `owner_email` from `config.yaml`. Enter it to claim the show.
5. Optional: submit the same feed to Apple Podcasts Connect, YouTube Music and Amazon Music.
