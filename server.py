"""
OpenCode Session Viewer - Backend Server
Reads opencode.db and provides REST API for session browsing.
Usage: python server.py [--port PORT] [--db PATH]
"""
import sqlite3
import json
import hashlib
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

SETTINGS_PATH = os.environ.get(
    "OPENCODE_VIEWER_SETTINGS",
    os.path.join(
        os.path.expanduser("~"), ".local", "share", "opencode",
        "session-viewer.json",
    ),
)

# ─── Database helper ─────────────────────────────────────────
def get_db(path=None):
    """Get a new SQLite connection with row factory."""
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ─── Database sources / settings ─────────────────────────────
settings_lock = threading.Lock()


def _db_id(path):
    digest = hashlib.sha1(
        os.path.normcase(os.path.abspath(path)).encode("utf-8")
    ).hexdigest()
    return f"db_{digest[:12]}"


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}

    custom = []
    if isinstance(data, dict):
        for item in data.get("custom", []):
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path.strip():
                continue
            custom.append({
                "id": item.get("id") or _db_id(path),
                "path": path,
                "enabled": bool(item.get("enabled", True)),
            })

    return {
        "default_enabled": bool(data.get("default_enabled", True))
        if isinstance(data, dict) else True,
        "custom": custom,
    }


def save_settings(settings):
    directory = os.path.dirname(SETTINGS_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)


def _custom_db_label(index):
    return f"拟{index + 1}"


def get_databases():
    settings = load_settings()
    databases = [{
        "id": "default",
        "label": "默认",
        "path": DB_PATH,
        "enabled": settings["default_enabled"],
        "builtin": True,
        "exists": os.path.isfile(DB_PATH),
        "index": 0,
    }]

    seen = {os.path.normcase(os.path.abspath(DB_PATH))}
    custom_index = 0
    for item in settings["custom"]:
        key = os.path.normcase(os.path.abspath(item["path"]))
        if key in seen:
            continue
        seen.add(key)
        databases.append({
            "id": item["id"],
            "label": _custom_db_label(custom_index),
            "path": item["path"],
            "enabled": item["enabled"],
            "builtin": False,
            "exists": os.path.isfile(item["path"]),
            "index": custom_index + 1,
        })
        custom_index += 1

    return databases


def get_enabled_databases():
    return [
        database for database in get_databases()
        if database["enabled"] and database["exists"]
    ]


def resolve_db_path(db_id):
    if not db_id or db_id == "default":
        return DB_PATH
    for database in get_databases():
        if database["id"] == db_id:
            return database["path"]
    return None


def is_opencode_db(path):
    if not os.path.isfile(path):
        return False
    try:
        conn = sqlite3.connect(path, timeout=2)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='session'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def update_settings(payload):
    action = payload.get("action")

    with settings_lock:
        settings = load_settings()

        if action == "set_all":
            enabled = bool(payload.get("enabled"))
            settings["default_enabled"] = enabled
            for item in settings["custom"]:
                item["enabled"] = enabled

        elif action == "set_enabled":
            db_id = payload.get("id")
            enabled = bool(payload.get("enabled"))
            if db_id == "default":
                settings["default_enabled"] = enabled
            else:
                for item in settings["custom"]:
                    if item["id"] == db_id:
                        item["enabled"] = enabled
                        break
                else:
                    return {"error": "数据库不存在"}

        elif action == "add":
            raw = str(payload.get("path") or "").strip().strip('"').strip("'")
            if not raw:
                return {"error": "请填写数据库路径"}
            path = os.path.abspath(os.path.expanduser(raw))
            if not os.path.isfile(path):
                return {"error": "文件不存在"}
            if not is_opencode_db(path):
                return {"error": "不是有效的 OpenCode 数据库"}

            existing = {os.path.normcase(os.path.abspath(DB_PATH))}
            existing |= {
                os.path.normcase(os.path.abspath(item["path"]))
                for item in settings["custom"]
            }
            if os.path.normcase(path) in existing:
                return {"error": "该数据库已存在"}

            settings["custom"].append({
                "id": _db_id(path),
                "path": path,
                "enabled": True,
            })

        elif action == "remove":
            db_id = payload.get("id")
            if db_id == "default":
                return {"error": "默认数据库不能删除"}
            before = len(settings["custom"])
            settings["custom"] = [
                item for item in settings["custom"] if item["id"] != db_id
            ]
            if len(settings["custom"]) == before:
                return {"error": "数据库不存在"}

        else:
            return {"error": "未知操作"}

        save_settings(settings)

    return {"databases": get_databases()}

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
def _like_pattern(value):
    """Build a contains-pattern with LIKE wildcards escaped."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _flag(params, name):
    return params.get(name, ["0"])[0].lower() in ("1", "true", "on", "yes")


def _session_dict(row, database, db_total):
    return {
        "id": row["id"],
        "title": row["title"],
        "directory": row["directory"],
        "path": row["path"],
        "agent": row["agent"],
        "model": json.loads(row["model"]) if row["model"] else None,
        "cost": row["cost"],
        "tokens_input": row["tokens_input"],
        "tokens_output": row["tokens_output"],
        "tokens_reasoning": row["tokens_reasoning"],
        "tokens_cache_read": row["tokens_cache_read"],
        "tokens_cache_write": row["tokens_cache_write"],
        "summary_additions": row["summary_additions"],
        "summary_deletions": row["summary_deletions"],
        "summary_files": row["summary_files"],
        "version": row["version"],
        "time_created": row["time_created"],
        "time_updated": row["time_updated"],
        "time_archived": row["time_archived"],
        "message_count": row["message_count"],
        "user_message_count": row["user_message_count"],
        "db": database["id"],
        "db_label": database["label"],
        "db_index": database.get("index", 0),
        "db_total": db_total,
    }


def api_sessions(params):
    """GET /api/sessions?page=1&limit=10&sort=desc&q=search&content=1&children=1"""
    page = int(params.get("page", [1])[0])
    limit = int(params.get("limit", [10])[0])
    sort = params.get("sort", ["desc"])[0]
    query = params.get("q", [""])[0].strip()
    search_content = _flag(params, "content")
    include_children = _flag(params, "children")
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

    # Build WHERE clause for search
    clauses = []
    where_params = []
    if query:
        like_q = _like_pattern(query)
        search_parts = [
            "s.id LIKE ? ESCAPE '\\'",
            "s.title LIKE ? ESCAPE '\\'",
        ]
        where_params.extend([like_q, like_q])

        if search_content:
            search_parts.append("""s.id IN (
                SELECT p.session_id FROM part p
                WHERE COALESCE(json_extract(p.data, '$.text'), '') LIKE ? ESCAPE '\\'
                   OR COALESCE(json_extract(p.data, '$.state.input'), '') LIKE ? ESCAPE '\\'
                   OR COALESCE(json_extract(p.data, '$.state.output'), '') LIKE ? ESCAPE '\\'
                   OR COALESCE(json_extract(p.data, '$.filename'), '') LIKE ? ESCAPE '\\'
                   OR COALESCE(json_extract(p.data, '$.files'), '') LIKE ? ESCAPE '\\'
            )""")
            where_params.extend([like_q] * 5)
            search_parts.append("""s.id IN (
                SELECT sm.session_id FROM session_message sm
                WHERE COALESCE(json_extract(sm.data, '$.text'), '') LIKE ? ESCAPE '\\'
            )""")
            where_params.append(like_q)

        clauses.append("(" + " OR ".join(search_parts) + ")")

    if not include_children:
        clauses.append("(s.parent_id IS NULL OR s.parent_id = '')")

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sessions = []
    all_databases = get_databases()
    db_total = len(all_databases)
    databases = [
        database for database in all_databases
        if database["enabled"] and database["exists"]
    ]
    for database in databases:
        try:
            conn = get_db(database["path"])
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(f"""
                SELECT s.*,
                       (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) as message_count,
                       (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id AND json_extract(m.data, '$.role')='user') as user_message_count
                FROM session s
                {where_clause}
                ORDER BY {order_clause}
            """, where_params).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()

        for row in rows:
            try:
                sessions.append(_session_dict(row, database, db_total))
            except (KeyError, TypeError, ValueError):
                continue

    if sort == "asc":
        sessions.sort(key=lambda s: s["time_created"] or 0)
    elif sort == "user_msg_desc":
        sessions.sort(key=lambda s: (
            -(s["user_message_count"] or 0), -(s["time_created"] or 0)
        ))
    elif sort == "user_msg_asc":
        sessions.sort(key=lambda s: (
            s["user_message_count"] or 0, -(s["time_created"] or 0)
        ))
    else:
        sessions.sort(key=lambda s: -(s["time_created"] or 0))

    total = len(sessions)
    page_sessions = sessions[offset:offset + limit]

    return {
        "sessions": page_sessions,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": max(1, (total + limit - 1) // limit),
        "database_count": len(databases),
    }

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

    def _send_file(self, path, filename, content_type="application/octet-stream"):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

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

            if path == "/api/settings":
                self._send_json({"databases": get_databases()})
                return

            if path == "/api/download-db":
                api_download_db(self, params)
                return

            if path.startswith("/api/sessions/") and path.endswith("/export"):
                session_id = path.split("/")[3]  # /api/sessions/{id}/export
                api_export_session(self, session_id)
                return

            if path.startswith("/api/sessions/") and path.endswith("/conversation"):
                session_id = path.split("/")[3]  # /api/sessions/{id}/conversation
                self._send_json(api_session_conversation(session_id, params))
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
        is_settings = path == "/api/settings"
        if path != "/api/open-folder" and not is_fork and not is_open and not is_settings:
            self._send_error(404, "Not Found")
            return

        if not self._is_local_request():
            self._send_error(403, "此操作仅限本机访问")
            return

        if is_settings:
            payload = self._read_json_body()
            if payload is None:
                return
            result = update_settings(payload)
            if "error" in result:
                self._send_error(400, result["error"])
            else:
                self._send_json(result)
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


def api_download_db(handler, params):
    """GET /api/download-db?db=<id> — download a database file."""
    db_id = params.get("db", ["default"])[0]
    db_path = resolve_db_path(db_id)
    if not db_path or not os.path.isfile(db_path):
        handler._send_error(404, "数据库不存在")
        return

    try:
        conn = get_db(db_path)
        try:
            # Fold pending WAL pages into the main file so the download is complete.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

        stem = os.path.splitext(os.path.basename(db_path))[0] or "opencode"
        stem = "".join(
            ch for ch in stem if ch.isascii() and (ch.isalnum() or ch in "-_")
        ) or "opencode"
        filename = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"
        handler._send_file(db_path, filename, "application/vnd.sqlite3")
    except Exception as e:
        handler._send_error(500, f"下载数据库失败: {e}")


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
    db_path = resolve_db_path(params.get("db", ["default"])[0])
    if not db_path or not os.path.isfile(db_path):
        return {"messages": [], "error": "数据库不存在"}

    conn = get_db(db_path)
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
        try:
            sm_rows = conn.execute("""
                SELECT * FROM session_message
                WHERE session_id = ? AND type = 'user'
                ORDER BY time_created DESC
                LIMIT ?
            """, (session_id, limit)).fetchall()
        except sqlite3.OperationalError:
            sm_rows = []

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


def _clip_text(value, limit=6000):
    """Render a part field as readable text, truncating very large payloads."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > limit:
        return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"
    return text


def _conversation_part(part_data):
    """Shape a part JSON blob into the fields the conversation modal renders."""
    ptype = part_data.get("type")

    if ptype in ("text", "reasoning"):
        text = part_data.get("text") or ""
        if not text.strip():
            return None
        return {"type": ptype, "text": text}

    if ptype == "tool":
        state = part_data.get("state") or {}
        output = state.get("output")
        if output is None and state.get("status") == "error":
            output = state.get("error")
        return {
            "type": "tool",
            "tool": part_data.get("tool") or "tool",
            "status": state.get("status") or "unknown",
            "title": state.get("title"),
            "input": _clip_text(state.get("input")),
            "output": _clip_text(output),
        }

    if ptype == "file":
        return {
            "type": "file",
            "filename": part_data.get("filename") or "文件",
            "mime": part_data.get("mime"),
        }

    if ptype == "patch":
        return {
            "type": "patch",
            "files": part_data.get("files") or [],
            "hash": part_data.get("hash"),
        }

    return None


def _message_error_text(error):
    if not error:
        return None
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and data.get("message"):
            return data["message"]
        return error.get("name") or "发生错误"
    return str(error)


def api_session_conversation(session_id, params):
    """GET /api/sessions/{id}/conversation — full ordered conversation record."""
    db_path = resolve_db_path(params.get("db", ["default"])[0])
    if not db_path or not os.path.isfile(db_path):
        return {"error": "数据库不存在"}

    conn = get_db(db_path)
    try:
        srow = conn.execute(
            "SELECT * FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if not srow:
            return {"error": "会话不存在"}

        rows = conn.execute("""
            SELECT m.id AS message_id, m.time_created AS message_time,
                   m.data AS message_data,
                   p.id AS part_id, p.time_created AS part_time,
                   p.data AS part_data
            FROM message m
            LEFT JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY m.time_created ASC, p.time_created ASC, p.id ASC
        """, (session_id,)).fetchall()

        messages = []
        by_id = {}
        for r in rows:
            mid = r["message_id"]
            if mid not in by_id:
                mdata = json.loads(r["message_data"]) if r["message_data"] else {}
                entry = {
                    "id": mid,
                    "role": mdata.get("role") or "assistant",
                    "agent": mdata.get("agent"),
                    "model": mdata.get("modelID"),
                    "time_created": r["message_time"],
                    "cost": mdata.get("cost"),
                    "tokens": mdata.get("tokens"),
                    "error": _message_error_text(mdata.get("error")),
                    "parts": [],
                }
                by_id[mid] = entry
                messages.append(entry)
            if r["part_data"]:
                try:
                    part = _conversation_part(json.loads(r["part_data"]))
                except (TypeError, ValueError):
                    part = None
                if part:
                    by_id[mid]["parts"].append(part)

        # Newer session_message store; skip ids already covered by legacy rows.
        try:
            sm_rows = conn.execute("""
                SELECT * FROM session_message
                WHERE session_id = ?
                ORDER BY seq ASC, time_created ASC
            """, (session_id,)).fetchall()
        except sqlite3.OperationalError:
            sm_rows = []

        for sm in sm_rows:
            if sm["id"] in by_id:
                continue
            try:
                sm_data = json.loads(sm["data"]) if sm["data"] else {}
            except (TypeError, ValueError):
                sm_data = {}
            text = sm_data.get("text")
            role = sm["type"] if sm["type"] in ("user", "assistant") else "assistant"
            entry = {
                "id": sm["id"],
                "role": role,
                "agent": None,
                "model": None,
                "time_created": sm["time_created"],
                "cost": None,
                "tokens": None,
                "error": None,
                "parts": [{"type": "text", "text": text}] if text else [],
            }
            by_id[sm["id"]] = entry
            messages.append(entry)

        messages.sort(key=lambda m: m["time_created"] or 0)
        messages = [m for m in messages if m["parts"] or m["error"]]

        session_meta = {
            "id": srow["id"],
            "title": srow["title"],
            "directory": srow["directory"],
            "agent": srow["agent"],
            "model": json.loads(srow["model"]) if srow["model"] else None,
            "time_created": srow["time_created"],
            "time_updated": srow["time_updated"],
        }

        return {"session": session_meta, "messages": messages}
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
