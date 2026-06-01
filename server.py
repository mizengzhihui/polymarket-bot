"""
Polymarket Copy-Trading Bot Dashboard Server
Serves web/ for HTML, results/ for data, /api/ for JSON endpoints.
"""
import json, os, time, http.server, urllib.parse, socketserver, subprocess

CONTROL_SECRET = os.environ.get("CONTROL_SECRET", "")
SERVICES = {"bot": "poly-bot", "server": "poly-server"}


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


PORT = 18766
ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.realpath(os.path.join(ROOT, "web"))
RESULTS_DIR = os.path.realpath(os.path.join(ROOT, "results"))


def load_bot_status():
    path = os.path.join(RESULTS_DIR, "bot_status.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"status": "stopped", "error": "bot_status.json not found"}


def health_check():
    """Check if bot is alive based on bot_status.json freshness.
    Returns healthy=False if last update > 2x poll interval (30s conservative)."""
    path = os.path.join(RESULTS_DIR, "bot_status.json")
    if not os.path.exists(path):
        return {"healthy": False, "reason": "bot_status.json not found"}
    try:
        mtime = os.path.getmtime(path)
        age_s = time.time() - mtime
        stale_s = 15  # 3x the 5s poll interval
        if age_s > stale_s:
            return {"healthy": False, "reason": f"last update {age_s:.0f}s ago (stale > {stale_s}s)"}
        return {"healthy": True, "last_update_s": round(age_s, 1)}
    except Exception:
        return {"healthy": False, "reason": "error reading status"}


# Whitelist: only serve files from these directories
SERVE_DIRS = {
    ".html": WEB_DIR,
    ".js": WEB_DIR,
    ".css": WEB_DIR,
    ".svg": WEB_DIR,
    ".png": WEB_DIR,
    ".ico": WEB_DIR,
    ".json": RESULTS_DIR,
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/bot":
            self.send_json(load_bot_status())
            return

        if path == "/api/health":
            self.send_json(health_check())
            return

        if path == "/":
            self.send_redirect("/dashboard.html")
            return

        # Only serve whitelisted extensions from their respective dirs
        ext = os.path.splitext(path)[1].lower()
        if ext in SERVE_DIRS:
            base_dir = SERVE_DIRS[ext]
            fpath = os.path.realpath(os.path.join(base_dir, path.lstrip("/")))
            if fpath.startswith(base_dir + os.sep) and os.path.exists(fpath):
                content_type = "text/html; charset=utf-8" if ext == ".html" else None
                if content_type is None and ext == ".json":
                    content_type = "application/json; charset=utf-8"
                self._serve_file(fpath, content_type)
                return
            self.send_error(404, "File not found")
            return

        self.send_error(404, "File not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/control":
            self.send_response(405); self.end_headers(); return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json_resp(400, {"error": "invalid json"}); return

        secret = body.get("secret", "")
        if not CONTROL_SECRET:
            self.send_json_resp(500, {"error": "CONTROL_SECRET not configured"}); return
        if secret != CONTROL_SECRET:
            self.send_json_resp(401, {"error": "unauthorized"}); return

        target = body.get("target", "bot")
        service = SERVICES.get(target)
        if not service:
            self.send_json_resp(400, {"error": f"unknown target: {target}"}); return

        action = body.get("action", "")
        if action not in ("start", "stop", "restart"):
            self.send_json_resp(400, {"error": "invalid action"}); return

        try:
            subprocess.run(["systemctl", action, service], check=True, timeout=10)
            self.send_json_resp(200, {"ok": True, "message": f"{target} {action}ed"})
        except subprocess.CalledProcessError as e:
            self.send_json_resp(500, {"error": f"systemctl failed: {e}"})
        except subprocess.TimeoutExpired:
            self.send_json_resp(504, {"error": "systemctl timed out"})

    def send_json_resp(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, content_type=None):
        try:
            with open(path, "rb") as f:
                body = f.read()
            if content_type is None:
                ext = os.path.splitext(path)[1].lower()
                content_type = {
                    ".html": "text/html; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".json": "application/json; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                    ".ico": "image/x-icon",
                }.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_error(500, "Internal server error")

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


def main():
    print("=" * 50)
    print("  Poly_Copy Dashboard Server")
    print(f"  http://localhost:{PORT}")
    print("=" * 50)
    print(f"\n  [ON] /dashboard.html  -> Copy Bot Dashboard")
    print(f"  API: /api/bot")
    print(f"\n  Press Ctrl+C to stop.\n")
    ThreadedHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
