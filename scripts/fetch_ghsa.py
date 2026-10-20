"""Download GitHub security advisories per ecosystem."""
import json, os, sys, time, urllib.request, urllib.error, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "security"
TOKEN = (ROOT / ".github_token").read_text().strip()
ECOS = ["npm", "pip", "go"]


def get(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "foresightCoder-ghsa",
    })
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r), r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(5, int(reset) - int(time.time()) + 2) if reset else 60
                print(f"  rate limited, sleeping {wait}s", flush=True)
                time.sleep(min(wait, 900))
                continue
            raise
        except Exception as exc:
            print(f"  retry {attempt}: {exc}", flush=True)
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"gave up on {url}")


def next_url(link):
    for part in link.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


for eco in ECOS:
    path = OUT / f"ghsa_{eco}.jsonl"
    url = f"https://api.github.com/advisories?ecosystem={eco}&per_page=100&sort=published&direction=desc"
    n, pages = 0, 0
    with path.open("w") as fh:
        while url:
            batch, link = get(url)
            for adv in batch:
                fh.write(json.dumps(adv) + "\n")
            n += len(batch)
            pages += 1
            if pages % 10 == 0:
                print(f"{eco}: {n} advisories ({pages} pages)", flush=True)
            url = next_url(link)
    print(f"DONE {eco}: {n} advisories -> {path}", flush=True)
