"""Soak test: run bot in dry-run for N seconds, count all API calls."""
import sys, os, time, json, re
sys.path.insert(0, "/opt/poly_copy")
from dotenv import load_dotenv; load_dotenv("/opt/poly_copy/.env")
from config import *

# Monkey-patch to count API calls
_orig_get = __import__('requests').get
_orig_post = __import__('requests').post
_calls = []
def _track(method, url, **kw):
    _calls.append({"t": time.time(), "m": method, "u": url[:80]})
    if method == "GET":
        return _orig_get(url, **kw)
    else:
        return _orig_post(url, **kw)
import requests
requests.get = lambda url, **kw: _track("GET", url, **kw)
requests.post = lambda url, **kw: _track("POST", url, **kw)

# Also monkey-patch urllib for orderbook calls
import urllib.request as _ur
_orig_urlopen = _ur.urlopen
def _track_urlopen(req, **kw):
    url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
    _calls.append({"t": time.time(), "m": "GET", "u": url[:80]})
    return _orig_urlopen(req, **kw)
_ur.urlopen = _track_urlopen

# Now run the bot in dry-run mode
from bot import run_bot

print("=" * 60)
print("  SOAK TEST — 180s dry-run")
print("=" * 60)

start = time.time()
try:
    # Run in background thread, kill after 180s
    import threading
    def runner():
        run_bot(dry_run=True, poll_interval=10)
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    time.sleep(180)
except KeyboardInterrupt:
    pass

elapsed = time.time() - start

# Analyze calls
print(f"\nElapsed: {elapsed:.0f}s, Total API calls: {len(_calls)}")

# Group by endpoint
from collections import Counter
endpoints = Counter()
for c in _calls:
    url = c["u"]
    # Normalize
    url = re.sub(r'token_id=[^&]+', 'token_id=XXX', url)
    url = re.sub(r'user=0x[a-fA-F0-9]+', 'user=WALLET', url)
    url = re.sub(r'/[a-f0-9]{40,}', '/HASH', url)
    endpoints[url[:100]] += 1

print("\n--- API Call Breakdown ---")
total = sum(endpoints.values())
for ep, count in endpoints.most_common():
    pct = count / total * 100
    per_h = count / elapsed * 3600
    print(f"  {count:4d} ({pct:5.1f}% | {per_h:6.0f}/h)  {ep[:90]}")

print(f"\n--- Summary ---")
print(f"Total calls: {total}")
print(f"Calls/hour: {total/elapsed*3600:.0f}")

# Check for redundant calls
# trader_value should be called ~1 time per 300s
tv_calls = sum(1 for c in _calls if '/value' in c['u'])
print(f"\ntrader_value calls: {tv_calls} (expected ~{elapsed/300:.0f} for 5min interval)")

# positions should only be called periodically
pos_calls = sum(1 for c in _calls if '/positions' in c['u'])
print(f"positions calls: {pos_calls}")

# trades calls
trade_calls = sum(1 for c in _calls if '/trades' in c['u'])
print(f"trades calls: {trade_calls}")

# order book calls
book_calls = sum(1 for c in _calls if '/book' in c['u'])
print(f"order book calls: {book_calls}")

# Save report
with open("/opt/poly_copy/results/soak_test.json", "w") as f:
    json.dump({
        "elapsed_s": elapsed,
        "total_calls": total,
        "calls_per_hour": round(total/elapsed*3600),
        "breakdown": {ep: {"count": c, "per_hour": round(c/elapsed*3600)} for ep, c in endpoints.most_common()},
    }, f, indent=2)
print("\nReport saved to results/soak_test.json")
