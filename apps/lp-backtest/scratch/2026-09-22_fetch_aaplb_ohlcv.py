import json, time, urllib.request, sys

POOL = "0xe9b9998b2ec5430d2246c7f1f8d9f298c97d7365"
NETWORK = "bsc"
START_TS = 1789732304  # 2026-09-18T09:31:44Z approx, will trim later precisely
END_NOW = int(time.time())

def fetch(before=None):
    url = f"https://api.geckoterminal.com/api/v2/networks/{NETWORK}/pools/{POOL}/ohlcv/minute?aggregate=5&limit=300&currency=token"
    if before:
        url += f"&before_timestamp={before}"
    req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

all_candles = {}
before = None
for page in range(10):
    d = fetch(before)
    lst = d["data"]["attributes"]["ohlcv_list"]
    if not lst:
        break
    for c in lst:
        all_candles[c[0]] = c
    oldest = min(c[0] for c in lst)
    print(f"page {page}: {len(lst)} candles, oldest={oldest}", file=sys.stderr)
    if oldest <= START_TS:
        break
    before = oldest
    time.sleep(2.2)

times = sorted(all_candles.keys())
out = [all_candles[t] for t in times]
with open("/private/tmp/claude-501/-Users-chenhouzhen-project-codes-bots-alpha-engine/8166843c-873a-48e9-a7f7-cdf80271aa39/scratchpad/aaplb_5m.json", "w") as f:
    json.dump(out, f)
print(f"total candles: {len(out)}, span {times[0]} .. {times[-1]}", file=sys.stderr)
