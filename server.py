"""
OpenCode Session Viewer - Backend Server
Reads opencode.db and provides REST API for session browsing.
Usage: python server.py [--port PORT] [--db PATH]
"""
import sqlite3
import json
import os
import sys
import shutil
import shlex
import socket
import subprocess
import threading
import time
import atexit
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode, quote

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

def open_directory(path):
    """Open a folder in the platform file manager."""
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ─── opencode server bridge (used for forking) ───────────────
_server_lock = threading.RLock()
_server_process = None
_server_port = None
_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _find_opencode():
    """Locate the opencode executable, preferring a real binary over wrappers."""
    if os.name != "nt":
        return shutil.which("opencode")

    exe = shutil.which("opencode.exe")
    if exe:
        return exe

    wrapper = shutil.which("opencode")
    if not wrapper:
        return None

    # npm installs a .cmd/.ps1 wrapper; run the binary it points to directly.
    candidate = os.path.join(
        os.path.dirname(wrapper), "node_modules", "opencode-ai", "bin", "opencode.exe"
    )
    return candidate if os.path.isfile(candidate) else wrapper


def _free_local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop_opencode_server():
    global _server_process, _server_port
    with _server_lock:
        process = _server_process
        _server_process = None
        _server_port = None

    if not process or process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return
        except Exception:
            pass

    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


atexit.register(_stop_opencode_server)


def _start_opencode_server():
    command = _find_opencode()
    if not command:
        raise RuntimeError("未找到 opencode 命令，请确认已安装并在 PATH 中")

    port = _free_local_port()
    env = os.environ.copy()
    env["OPENCODE_DB"] = os.path.abspath(DB_PATH)
    env.pop("OPENCODE_SERVER_PASSWORD", None)
    env.pop("OPENCODE_SERVER_USERNAME", None)

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    process = subprocess.Popen(
        [command, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        **kwargs,
    )

    ready_url = f"http://127.0.0.1:{port}/doc"
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("opencode 服务启动失败")
        try:
            with _local_opener.open(ready_url, timeout=1) as response:
                if response.status == 200:
                    return process, port
        except Exception:
            pass
        time.sleep(0.2)

    try:
        process.terminate()
    except Exception:
        pass
    raise RuntimeError("opencode 服务启动超时")


def _get_opencode_server():
    global _server_process, _server_port
    with _server_lock:
        if _server_process and _server_process.poll() is None and _server_port:
            return _server_process, _server_port
        try:
            _stop_opencode_server()
        except Exception:
            pass
        process, port = _start_opencode_server()
        _server_process = process
        _server_port = port
        return process, port


def _launch_terminal(command, session_id, directory, env):
    """Open a terminal window running `opencode --session <id>`."""
    args = [command, "--session", session_id]

    if os.name == "nt":
        subprocess.Popen(
            args,
            cwd=directory,
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return

    if sys.platform == "darwin":
        quoted_dir = shlex.quote(directory or os.getcwd())
        quoted_cmd = shlex.quote(command)
        quoted_session = shlex.quote(session_id)
        script = (
            'tell application "Terminal" to do script '
            f'"cd {quoted_dir} && {quoted_cmd} --session {quoted_session}"'
        )
        subprocess.Popen(["osascript", "-e", script], env=env)
        return

    terminals = [
        ("x-terminal-emulator", ["-e"]),
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("xfce4-terminal", ["-e"]),
        ("xterm", ["-e"]),
    ]
    for name, prefix in terminals:
        terminal = shutil.which(name)
        if not terminal:
            continue
        subprocess.Popen([terminal, *prefix, *args], cwd=directory, env=env)
        return

    raise RuntimeError("未找到可用的终端程序")


# ─── API Handlers ─────────────────────────────────────────────
def api_sessions(params):
    """GET /api/sessions?page=1&limit=10&sort=desc&q=search"""
    page = int(params.get("page", [1])[0])
    limit = int(params.get("limit", [10])[0])
    sort = params.get("sort", ["desc"])[0]
    query = params.get("q", [""])[0].strip()
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
        # Build WHERE clause for search
        where_clause = ""
        where_params = []
        if query:
            where_clause = "WHERE s.id LIKE ? OR s.title LIKE ?"
            like_q = f"%{query}%"
            where_params = [like_q, like_q]

        # Total count (filtered)
        total = conn.execute(
            f"SELECT COUNT(*) FROM session s {where_clause}", where_params
        ).fetchone()[0]

        # Sessions with message counts
        rows = conn.execute(f"""
            SELECT s.*,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) as message_count,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id AND json_extract(m.data, '$.role')='user') as user_message_count
            FROM session s
            {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """, where_params + [limit, offset]).fetchall()

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
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def _is_local_request(self):
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False

        origin = self.headers.get("Origin")
        if origin:
            origin_host = urlparse(origin).netloc
            request_host = self.headers.get("Host", "")
            if origin_host != request_host:
                return False
        return True

    def _read_json_body(self, max_bytes=4096):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_error(400, "Invalid request body")
            return None

        if length <= 0 or length > max_bytes:
            self._send_error(400, "Invalid request body")
            return None

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as e:
            self._send_error(400, f"Invalid request: {e}")
            return None

        if not isinstance(payload, dict):
            self._send_error(400, "Invalid request body")
            return None
        return payload

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        try:
            # API routes
            if path == "/api/sessions":
                self._send_json(api_sessions(params))
                return

            if path.startswith("/api/sessions/") and path.endswith("/export"):
                session_id = path.split("/")[3]  # /api/sessions/{id}/export
                api_export_session(self, session_id)
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

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        is_fork = path.startswith("/api/sessions/") and path.endswith("/fork")
        is_open = path.startswith("/api/sessions/") and path.endswith("/open")
        if path != "/api/open-folder" and not is_fork and not is_open:
            self._send_error(404, "Not Found")
            return

        if not self._is_local_request():
            self._send_error(403, "此操作仅限本机访问")
            return

        if is_fork or is_open:
            parts = path.strip("/").split("/")
            if len(parts) != 4 or not parts[2]:
                self._send_error(404, "Not Found")
                return
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                if is_fork:
                    api_fork_session(self, parts[2], payload)
                else:
                    api_open_session(self, parts[2])
            except Exception as e:
                self._send_error(500, str(e))
            return

        payload = self._read_json_body()
        if payload is None:
            return

        try:
            folder = payload.get("path")
            if not isinstance(folder, str) or not folder.strip():
                self._send_error(400, "Missing folder path")
                return

            folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder.strip())))
            if os.path.isfile(folder):
                folder = os.path.dirname(folder)
            if not os.path.isdir(folder):
                self._send_error(404, "Folder not found")
                return

            open_directory(folder)
            self._send_json({"ok": True, "path": folder})
        except Exception as e:
            self._send_error(500, str(e))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ViewerHTTPServer(ThreadingHTTPServer):
    """HTTP server that refuses to share its port with another instance."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            # Without this, Windows lets a second process bind the same port,
            # so stale instances keep answering requests on the old code.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def api_export_session(handler, session_id):
    """GET /api/sessions/{id}/export — run opencode export and return JSON."""
    command = shutil.which("opencode")
    if not command:
        handler._send_error(500, "opencode CLI not found. Is it installed?")
        return

    try:
        result = subprocess.run(
            [command, "export", session_id],
            capture_output=True, timeout=120,
            shell=False,
        )
        # opencode writes UTF-8 JSON to stdout, progress text to stderr.
        stdout = result.stdout.decode("utf-8-sig", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()

        if result.returncode != 0:
            detail = stderr[-600:] or f"exit code {result.returncode}"
            handler._send_error(500, f"opencode export failed: {detail}")
            return

        if not stdout:
            detail = stderr[-600:] or "no output"
            handler._send_error(500, f"opencode export returned empty output: {detail}")
            return

        data = json.loads(stdout)
        handler._send_json(data)
    except subprocess.TimeoutExpired:
        handler._send_error(504, "opencode export timed out after 120 seconds")
    except json.JSONDecodeError as e:
        handler._send_error(500, f"Invalid JSON from opencode: {e}")
    except Exception as e:
        handler._send_error(500, str(e))


def _opencode_error_detail(error):
    detail = error.read().decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(detail)
        if isinstance(parsed, dict):
            message = parsed.get("message") or parsed.get("error")
            if message is not None:
                detail = message if isinstance(message, str) else json.dumps(
                    message, ensure_ascii=False
                )
    except Exception:
        pass
    return detail[:400]


def _fetch_message_ids(port, session_id, directory):
    """Return every message ID of a session in chronological order."""
    url = f"http://127.0.0.1:{port}/session/{quote(session_id, safe='')}/message"
    if directory:
        url += f"?{urlencode({'directory': directory})}"

    request = urllib.request.Request(url, method="GET")
    with _local_opener.open(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))

    message_ids = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        message_id = (item.get("info") or {}).get("id")
        if isinstance(message_id, str):
            message_ids.append(message_id)
    return message_ids


def api_fork_session(handler, session_id, payload):
    """POST /api/sessions/{id}/fork — fork a session up to a user message (inclusive)."""
    message_id = payload.get("messageID")
    if not isinstance(message_id, str) or not message_id.startswith("msg"):
        handler._send_error(400, "缺少有效的 messageID")
        return

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT directory FROM session WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        handler._send_error(404, "会话不存在")
        return

    directory = row["directory"]

    try:
        _process, port = _get_opencode_server()
    except Exception as e:
        handler._send_error(500, f"无法启动 opencode 服务：{e}")
        return

    try:
        message_ids = _fetch_message_ids(port, session_id, directory)
    except urllib.error.HTTPError as e:
        handler._send_error(500, f"无法读取会话消息：{_opencode_error_detail(e)}")
        return
    except Exception as e:
        handler._send_error(500, f"无法读取会话消息：{e}")
        return

    try:
        index = message_ids.index(message_id)
    except ValueError:
        handler._send_error(400, "未找到该输入对应的消息，无法创建分支")
        return

    # opencode forks *before* the given message, so use the next message ID to
    # include the selected input. With no next message, fork the whole session.
    fork_payload = {}
    if index + 1 < len(message_ids):
        fork_payload["messageID"] = message_ids[index + 1]

    query = urlencode({"directory": directory}) if directory else ""
    url = f"http://127.0.0.1:{port}/session/{quote(session_id, safe='')}/fork"
    if query:
        url += f"?{query}"

    body = json.dumps(fork_payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with _local_opener.open(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        handler._send_json({"session": data})
    except urllib.error.HTTPError as e:
        handler._send_error(e.code, f"创建分支失败：{_opencode_error_detail(e)}")
    except Exception as e:
        handler._send_error(500, f"创建分支失败：{e}")


def api_open_session(handler, session_id):
    """POST /api/sessions/{id}/open — open this session in an opencode window."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT directory FROM session WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        handler._send_error(404, "会话不存在")
        return

    directory = row["directory"]
    if not directory or not os.path.isdir(directory):
        directory = None

    command = _find_opencode()
    if not command:
        handler._send_error(500, "未找到 opencode 命令，请确认已安装并在 PATH 中")
        return

    env = os.environ.copy()
    env["OPENCODE_DB"] = os.path.abspath(DB_PATH)

    try:
        _launch_terminal(command, session_id, directory, env)
        handler._send_json({"ok": True})
    except Exception as e:
        handler._send_error(500, f"无法打开 opencode：{e}")


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

    url = f"http://localhost:{PORT}"
    print(f"\n  📂 Database: {DB_PATH}")
    print(f"  🌐 Server:   {url}")
    print(f"  📋 Press Ctrl+C to stop\n")

    # Auto-open browser
    if not os.environ.get("OPENCODE_NO_BROWSER"):
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    try:
        server = ViewerHTTPServer(("0.0.0.0", PORT), RequestHandler)
    except OSError as e:
        print(f"\n  Cannot listen on port {PORT}: {e}")
        print("  Another viewer may already be running. Close it or use --port.\n")
        sys.exit(1)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped.")
        server.shutdown()


def _open_browser(url):
    """Open the default web browser after a short delay."""
    import time
    time.sleep(0.8)
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    main()
