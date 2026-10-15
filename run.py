#!/usr/bin/env python3
"""Cross-Chain Daily podcast pipeline.

  python run.py daily                 publish what cleared review, then make the next episode
  python run.py generate [--slug S] [--dry-run] [--count N]
  python run.py publish-due           publish pending episodes past their review window
  python run.py catalog               sync live blog posts + classify new ones
  python run.py plan [--n 50] [--no-judge]   preview the next N picks without making anything
  python run.py cast ID [--pick N] [--seed S]   audition / lock a character's Fish voice
  python run.py feed                  rebuild docs/feed.xml and docs/index.html
  python run.py cover                 rebuild docs/cover.jpg
"""
from __future__ import annotations

import argparse
from collections import Counter

from podcast import common, feed, picker


def cmd_plan(n: int, no_judge: bool) -> None:
    from podcast.catalog import refresh

    cfg = common.load_config()
    catalog = refresh()
    if no_judge:
        picker.judge = lambda shortlist, history, cfg: (shortlist[0], [])
    sim = [e for e in common.read_json(common.EPISODES_PATH, []) if e["status"] in picker.ACTIVE]
    start = len(sim)
    for _ in range(n):
        try:
            c = picker.choose(catalog, sim, cfg)
        except RuntimeError as e:
            print(f"stopped: {e}")
            break
        p = c["post"]
        sim.append({"number": len(sim) + 1, "slug": p["slug"], "status": "published", "cluster": p["cls"]["cluster"],
                    "category": p["cls"]["category"], "format": c["format"], "guest": c["guest"],
                    "title": p["title"], "summary": p["description"]})
        print(f"{len(sim):>3}  {c['format']:<10} {c['guest'] or '-':<6} {p['cls']['category']:<17} "
              f"{p.get('published') or '?':<10}  {p['title'][:70]}")
    new = sim[start:]
    clusters = Counter(e["cluster"] for e in new)
    print(f"\n{len(new)} episodes · {len(clusters)} distinct clusters · repeats: {[c for c, k in clusters.items() if k > 1] or 'none'}")
    print("formats:", dict(Counter(e["format"] for e in new)), "· categories:", dict(Counter(e["category"] for e in new)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daily")
    g = sub.add_parser("generate")
    g.add_argument("--slug")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--count", type=int, default=1)
    sub.add_parser("publish-due")
    sub.add_parser("catalog")
    p = sub.add_parser("plan")
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--no-judge", action="store_true")
    c = sub.add_parser("cast")
    c.add_argument("id")
    c.add_argument("--pick", type=int)
    c.add_argument("--seed", type=int)
    sub.add_parser("feed")
    sub.add_parser("cover")
    args = ap.parse_args()

    if args.cmd == "daily":
        from podcast.pipeline import daily
        daily()
    elif args.cmd == "generate":
        from podcast.pipeline import generate
        for _ in range(args.count):
            generate(args.slug, args.dry_run)
    elif args.cmd == "publish-due":
        from podcast.pipeline import publish_due
        publish_due()
    elif args.cmd == "catalog":
        from podcast.catalog import refresh
        cat = refresh()
        live = [p for p in cat.values() if p["live"] and p.get("cls")]
        print(f"{len(live)} live classified posts, {len({p['cls']['cluster'] for p in live})} clusters")
        print(dict(Counter(p["cls"]["category"] for p in live).most_common()))
    elif args.cmd == "plan":
        cmd_plan(args.n, args.no_judge)
    elif args.cmd == "cast":
        from podcast import voices
        if args.pick is None:
            out = voices.audition(args.id, seed=args.seed)
            print(f"Listen to {out}/candidate_*.wav, then: python run.py cast {args.id} --pick N")
        else:
            print(f"{args.id} voice_id = {voices.pick_voice(args.id, args.pick)} (saved to cast.yaml)")
    elif args.cmd == "feed":
        feed.build(common.read_json(common.EPISODES_PATH, []))
    elif args.cmd == "cover":
        from podcast import cover
        cover.build()


if __name__ == "__main__":
    main()
