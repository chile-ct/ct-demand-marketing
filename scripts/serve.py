"""
Local dashboard server — auto-updates in background when data is stale.
Usage: python3 scripts/serve.py
Opens http://localhost:8000
"""
import http.server, os, time, subprocess, threading, webbrowser

PORT      = 8000
DIST_DIR  = os.path.join(os.path.dirname(__file__), '..', 'dist')
SCRIPT    = os.path.join(os.path.dirname(__file__), 'update_marketplace.py')
STALE_SEC = 30 * 60  # trigger background update if file older than 30 min

_updating = False
_update_lock = threading.Lock()

def is_stale():
    try:
        html = os.path.join(DIST_DIR, 'index.html')
        return (time.time() - os.path.getmtime(html)) > STALE_SEC
    except OSError:
        return True

def run_update():
    global _updating
    with _update_lock:
        if _updating:
            return
        _updating = True
    try:
        print('[serve] Data stale — running update in background...')
        result = subprocess.run(
            ['python3', SCRIPT],
            capture_output=True, text=True,
            cwd=os.path.dirname(SCRIPT)
        )
        if result.returncode == 0:
            print('[serve] Update done. Reload to see latest data.')
        else:
            print('[serve] Update failed:\n', result.stderr[-500:])
    finally:
        with _update_lock:
            _updating = False

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # Trigger background update if stale (non-blocking — serves immediately)
        if is_stale() and not _updating:
            threading.Thread(target=run_update, daemon=True).start()
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # suppress per-request noise; update messages still print

os.chdir(DIST_DIR)
print(f'[serve] Dashboard at http://localhost:{PORT}')
print(f'[serve] Auto-updates when data > {STALE_SEC//60} min old (background, non-blocking)')
webbrowser.open(f'http://localhost:{PORT}')
http.server.HTTPServer(('', PORT), Handler).serve_forever()
