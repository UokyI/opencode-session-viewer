"""
OpenCode Session Viewer - Backend Server
Reads opencode.db and provides REST API for session browsing.
Usage: python server.py [--port PORT] [--db PATH]
"""
import sqlite3
import json
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─── Configuration ───────────────────────────────────────────
DB_PATH = os.environ.get(
    "OPENCODE_DB",
    os.path.expandvars(r"%USERPROFILE%\.local\share\opencode\opencode.db")
)
PORT = int(os.environ.get("PORT", 8080))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

# Parse CLI args
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--port" and i + 1 < len(args):
        PORT = int(args[i + 1]); i += 2
    elif args[i] == "--db" and i + 1 < len(args):
        DB_PATH = args[i + 1]; i += 2
    elif args[i] == "--help":
        print(__doc__)
        sys.exit(0)
    else:
        i += 1

# ─── Database helper ─────────────────────────────────────────
def get_db():
    """Get a new SQLite connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

# ─── API Handlers ─────────────────────────────────────────────
def api_sessions(params):
    """GET /api/sessions?page=1&limit=10&sort=desc"""
    page = int(params.get("page", [1])[0])
    limit = int(params.get("limit", [10])[0])
    sort = params.get("sort", ["desc"])[0]
    limit = min(max(limit, 1), 100)  # clamp 1-100
    offset = (page - 1) * limit

    # Map sort parameter to ORDER BY clause
    sort_map = {
        "desc":          "s.time_created DESC",
        "asc":           "s.time_created ASC",
        "user_msg_desc": "user_message_count DESC, s.time_created DESC",
        "user_msg_asc":  "user_message_count ASC, s.time_created DESC",
    }
    order_clause = sort_map.get(sort, "s.time_created DESC")

    conn = get_db()
    try:
        # Total count
        total = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]

        # Sessions with message counts
        rows = conn.execute(f"""
            SELECT s.*,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) as message_count,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id AND json_extract(m.data, '$.role')='user') as user_message_count
            FROM session s
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

        sessions = []
        for r in rows:
            sessions.append({
                "id": r["id"],
                "title": r["title"],
                "directory": r["directory"],
                "path": r["path"],
                "agent": r["agent"],
                "model": json.loads(r["model"]) if r["model"] else None,
                "cost": r["cost"],
                "tokens_input": r["tokens_input"],
                "tokens_output": r["tokens_output"],
                "tokens_reasoning": r["tokens_reasoning"],
                "tokens_cache_read": r["tokens_cache_read"],
                "tokens_cache_write": r["tokens_cache_write"],
                "summary_additions": r["summary_additions"],
                "summary_deletions": r["summary_deletions"],
                "summary_files": r["summary_files"],
                "version": r["version"],
                "time_created": r["time_created"],
                "time_updated": r["time_updated"],
                "time_archived": r["time_archived"],
                "message_count": r["message_count"],
                "user_message_count": r["user_message_count"],
            })

        return {
            "sessions": sessions,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": max(1, (total + limit - 1) // limit),
        }
    finally:
        conn.close()

def api_session_messages(params):
    """GET /api/sessions/:id/messages?limit=10"""
    session_id = params.get("_path_parts", [None])[-1] if "_path_parts" in params else None
    if not session_id:
        # Extract from URL path in a different way
        pass

    limit = int(params.get("limit", [10])[0])
    limit = min(max(limit, 1), 50)

    conn = get_db()
    try:
        # Get user messages (from legacy message+part tables)
        rows = conn.execute("""
            SELECT m.id, m.time_created, m.data as msg_data,
                   p.id as part_id, p.data as part_data
            FROM message m
            LEFT JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
              AND json_extract(m.data, '$.role') = 'user'
            ORDER BY m.time_created DESC
            LIMIT ?
        """, (session_id, limit)).fetchall()

        messages = []
        # Group parts by message
        msg_map = {}
        for r in rows:
            mid = r["id"]
            if mid not in msg_map:
                msg_data = json.loads(r["msg_data"])
                msg_map[mid] = {
                    "id": mid,
                    "time_created": r["time_created"],
                    "agent": msg_data.get("agent"),
                    "model": msg_data.get("model"),
                    "texts": [],
                }
            if r["part_data"]:
                part_data = json.loads(r["part_data"])
                if part_data.get("type") == "text":
                    msg_map[mid]["texts"].append(part_data.get("text", ""))
                elif part_data.get("type") == "file":
                    msg_map[mid]["texts"].append(f"[File: {part_data.get('filename', 'unknown')}]")

        # Also check session_message table for user type
        sm_rows = conn.execute("""
            SELECT * FROM session_message
            WHERE session_id = ? AND type = 'user'
            ORDER BY time_created DESC
            LIMIT ?
        """, (session_id, limit)).fetchall()

        for sm in sm_rows:
            sm_data = json.loads(sm["data"])
            msg_map[sm["id"]] = {
                "id": sm["id"],
                "time_created": sm["time_created"],
                "agent": None,
                "model": None,
                "texts": [sm_data.get("text", "")],
            }

        # Sort all messages by time, return as list
        messages = sorted(msg_map.values(), key=lambda x: x["time_created"], reverse=True)
        messages = messages[:limit]

        # Flatten texts
        for m in messages:
            m["text"] = " ".join(m["texts"]).strip()
            del m["texts"]

        return {"messages": messages}
    finally:
        conn.close()


# ─── HTTP Server ─────────────────────────────────────────────
class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content, status=200):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        try:
            # API routes
            if path == "/api/sessions":
                self._send_json(api_sessions(params))
                return

            if path.startswith("/api/sessions/") and path.endswith("/messages"):
                session_id = path.split("/")[3]  # /api/sessions/{id}/messages
                params["_session_id"] = session_id
                # Need a different approach...
                self._send_json(api_session_messages_by_id(session_id, params))
                return

            # Serve index.html
            if path == "/" or path == "/index.html":
                if os.path.exists(HTML_FILE):
                    with open(HTML_FILE, "r", encoding="utf-8") as f:
                        self._send_html(f.read())
                else:
                    self._send_html(INDEX_HTML_FALLBACK)
                return

            # 404
            self._send_error(404, "Not Found")
        except Exception as e:
            self._send_error(500, str(e))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def api_session_messages_by_id(session_id, params):
    """Handle /api/sessions/{id}/messages"""
    limit = int(params.get("limit", [10])[0])
    limit = min(max(limit, 1), 50)

    conn = get_db()
    try:
        msg_map = {}

        # Legacy messages
        rows = conn.execute("""
            SELECT m.id, m.time_created, m.data as msg_data,
                   p.id as part_id, p.data as part_data
            FROM message m
            LEFT JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
              AND json_extract(m.data, '$.role') = 'user'
            ORDER BY m.time_created DESC
            LIMIT ?
        """, (session_id, limit)).fetchall()

        for r in rows:
            mid = r["id"]
            if mid not in msg_map:
                msg_data = json.loads(r["msg_data"])
                msg_map[mid] = {
                    "id": mid,
                    "time_created": r["time_created"],
                    "agent": msg_data.get("agent"),
                    "model": msg_data.get("model"),
                    "texts": [],
                }
            if r["part_data"]:
                part_data = json.loads(r["part_data"])
                if part_data.get("type") == "text":
                    text = part_data.get("text", "")
                    if text:
                        msg_map[mid]["texts"].append(text)
                elif part_data.get("type") == "file":
                    fname = part_data.get("filename", "unknown")
                    msg_map[mid]["texts"].append(f"[File: {fname}]")

        # New session_message table
        sm_rows = conn.execute("""
            SELECT * FROM session_message
            WHERE session_id = ? AND type = 'user'
            ORDER BY time_created DESC
            LIMIT ?
        """, (session_id, limit)).fetchall()

        for sm in sm_rows:
            sm_data = json.loads(sm["data"])
            msg_map[sm["id"]] = {
                "id": sm["id"],
                "time_created": sm["time_created"],
                "agent": None,
                "model": None,
                "texts": [sm_data.get("text", "")],
            }

        messages = sorted(msg_map.values(), key=lambda x: x["time_created"], reverse=True)[:limit]

        for m in messages:
            m["text"] = " ".join(m["texts"]).strip()
            del m["texts"]

        return {"messages": messages}
    finally:
        conn.close()


# ─── Fallback HTML (if index.html not found) ─────────────────
INDEX_HTML_FALLBACK = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>OpenCode Sessions</title></head>
<body><h1>OpenCode Session Viewer</h1><p>Please place index.html in the same directory.</p></body>
</html>"""

# ─── Main ────────────────────────────────────────────────────
def main():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        print(f"Set OPENCODE_DB environment variable or use --db flag.")
        print(f"Example: python server.py --db C:\\path\\to\\opencode.db")
        sys.exit(1)

    print(f"📂 Database: {DB_PATH}")
    print(f"🌐 Server:   http://localhost:{PORT}")
    print(f"📋 Press Ctrl+C to stop")

    server = HTTPServer(("0.0.0.0", PORT), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
