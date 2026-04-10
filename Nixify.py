import datetime
import hashlib
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import string
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import yt_dlp

try:
    import vlc  # type: ignore
except Exception:
    vlc = None


APP_NAME = "Nixify"
FOOTER_CREDIT = "Cree par Ofwood"
DEFAULT_INVITE_CODE = os.environ.get("NIXIFY_CLUB_CODE", "ILOVENANO")
DEFAULT_SECRET_KEY = os.environ.get("NIXIFY_SECRET_KEY", "NIXIFY_KEY_SECRET_JUKEBOX_MASTER")
APP_BUILD = os.environ.get("APP_BUILD", "dev")
EXPECTED_ENTRYPOINT = os.environ.get("NIXIFY_ENTRYPOINT", "/root/nixify/Nixify.py")
STRICT_ENTRYPOINT = os.environ.get("NIXIFY_STRICT_ENTRYPOINT", "0") == "1"
SERVER_PORT = int(os.environ.get("NIXIFY_PORT", "8000"))
MAX_COOKIE_UPLOAD_BYTES = 1024 * 1024
MAX_COOKIE_UPLOAD_LABEL = "1 Mo"
MAX_TRACK_FILESIZE = 200 * 1024 * 1024
MAX_TRACK_FILESIZE_LABEL = "200 Mo"
MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_FOLDER_IMPORT_BYTES = 5 * 1024 * 1024 * 1024
MAX_FOLDER_IMPORT_LABEL = "5 Go"
MAX_UPLOAD_REQUEST_BYTES = MAX_FOLDER_IMPORT_BYTES
MAX_SHARED_QUEUE_SIZE = 50
MAX_TRACKS_PER_USER = 200
DEFAULT_PLAYBACK_VOLUME = max(0, min(100, int(os.environ.get("NIXIFY_DEFAULT_VOLUME", "85"))))
WEB_COVER_PREVIEW_SIZE = 256
WEB_COVER_PREVIEW_QUALITY = 45
WEB_COVER_CACHE_DIR = os.path.join("/tmp", "nixify-web-covers")
LIBRARY_SYNC_MIN_INTERVAL = 45
DISCORD_WEBHOOK_URL = os.environ.get(
    "NIXIFY_DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1491171898262818920/iba_uMuqGIURsGNtSAmiDldbhuFTk_ctvNjHkFWgnHNCWow-dX8jjsQi0lWF6ZnwI2Pr",
)
DISCORD_EMBED_COLOR = int("00f3ff", 16)
DISCORD_FOOTER_TEXT = "Propulsé par Nixify - Dev by Ofwood"
DISCORD_HTTP_USER_AGENT = "Mozilla/5.0 Nixify/1.0"
PUBLIC_BASE_URL = os.environ.get("NIXIFY_PUBLIC_BASE_URL", "").strip().rstrip("/")

app = Flask(__name__)
app.secret_key = DEFAULT_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_REQUEST_BYTES
try:
    app.json.ensure_ascii = False
except Exception:
    pass

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "users.db")
COOKIES_DIR = os.path.join(BASE_DIR, "user_cookies")
FFMPEG_PATH = os.path.join(BASE_DIR, "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")
EXTENSIONS_AUDIO = (".flac", ".mp3", ".wav", ".m4a", ".webm", ".ogg")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
ALLOWED_COOKIE_EXTENSIONS = {".txt"}
GENERIC_COVER_NAMES = {"cover.jpg", "cover.png", "folder.jpg", "thumb.jpg"}
NIXIFY_NAS_PATH = os.environ.get("NIXIFY_NAS_PATH", "/mnt/nixify_nas")

nas_is_active = os.path.ismount(NIXIFY_NAS_PATH) if platform.system() != "Windows" else os.path.exists(NIXIFY_NAS_PATH)
AUDIO_ROOT = NIXIFY_NAS_PATH if nas_is_active else os.path.join(BASE_DIR, "sons")

sync_status = {}
sync_status_lock = threading.Lock()
library_sync_lock = threading.Lock()
library_sync_timestamps = {}
library_sync_inflight = set()
local_ip_lock = threading.Lock()
cached_local_ip = None
cached_local_ip_score = -1


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)


def has_ffmpeg():
    local_ffmpeg = os.path.exists(FFMPEG_PATH)
    system_ffmpeg = shutil.which("ffmpeg") is not None
    system_ffprobe = shutil.which("ffprobe") is not None
    return local_ffmpeg or (system_ffmpeg and system_ffprobe)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def normalize_username(value):
    return (value or "").strip().lower()


def is_reserved_username(value):
    return normalize_username(value) == "admin"


def user_has_admin_access(user):
    if not user:
        return False
    return bool((user.role or "") == "admin" or is_reserved_username(user.username))


def generate_temporary_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_column(cursor, table_name, column_name, ddl):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = {row[1] for row in cursor.fetchall()}
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def init_db():
    ensure_directory(COOKIES_DIR)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            """
        )
        ensure_column(cursor, "users", "role", "role TEXT NOT NULL DEFAULT 'user'")
        ensure_column(cursor, "users", "playlist_url", "playlist_url TEXT")
        ensure_column(cursor, "users", "cookies_path", "cookies_path TEXT")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                url TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_entries (
                playlist_id INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                UNIQUE(playlist_id, file_path)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                logical_name TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                file_path TEXT NOT NULL,
                cover_path TEXT,
                duration REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE,
                UNIQUE(user_id, track_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                playlist_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE,
                UNIQUE(playlist_id, track_id)
            )
            """
        )
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_user_file_path ON tracks(user_id, file_path)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_user_logical_name ON tracks(user_id, logical_name COLLATE NOCASE)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_ci ON users(LOWER(username))")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_playlists_user_name ON playlists(user_id, name COLLATE NOCASE)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_playlist_tracks_position ON playlist_tracks(playlist_id, position)")

        cursor.execute("SELECT id, playlist_url FROM users")
        for user_id, playlist_url in cursor.fetchall():
            if playlist_url:
                continue
            row = cursor.execute(
                """
                SELECT url FROM playlists
                WHERE user_id = ? AND url IS NOT NULL AND TRIM(url) != ''
                ORDER BY id ASC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row and row[0]:
                cursor.execute("UPDATE users SET playlist_url = ? WHERE id = ?", (row[0], user_id))

        admin_count = cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        if admin_count == 0:
            first_user = cursor.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
            if first_user:
                cursor.execute("UPDATE users SET role = 'admin' WHERE id = ?", (first_user[0],))

        admin_usernames = {
            username.strip().lower()
            for username in os.environ.get("NIXIFY_ADMIN_USERS", "").split(",")
            if username.strip()
        }
        if admin_usernames:
            for user_id, username in cursor.execute("SELECT id, username FROM users").fetchall():
                if username.lower() in admin_usernames:
                    cursor.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))

        conn.commit()

    sync_library_database()


class User(UserMixin):
    def __init__(self, user_id, username, role, playlist_url=None, cookies_path=None):
        self.id = user_id
        self.username = username
        self.role = role or "user"
        self.playlist_url = playlist_url
        self.cookies_path = cookies_path

    @property
    def is_admin(self):
        return user_has_admin_access(self)

    @property
    def normalized_username(self):
        return normalize_username(self.username)

    @staticmethod
    def from_row(row):
        if not row:
            return None
        return User(
            user_id=row["id"],
            username=row["username"],
            role=row["role"],
            playlist_url=row["playlist_url"],
            cookies_path=row["cookies_path"],
        )

    @staticmethod
    def get(user_id):
        with get_db_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(row)

    @staticmethod
    def get_by_username(username):
        normalized = normalize_username(username)
        if not normalized:
            return None, None
        with get_db_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (normalized,)).fetchone()
        if not row:
            return None, None
        return User.from_row(row), row["password_hash"]


@login_manager.user_loader
def load_user(user_id):
    return User.get(int(user_id))


def category_from_rel_path(username, rel_path):
    relative_dir = os.path.dirname(rel_path or "")
    if not relative_dir:
        return "Bibliotheque"
    folder_name = os.path.basename(relative_dir)
    return folder_name or username


def normalized_name_key(value):
    return normalized_track_title(value).lower()


def sanitized_name_key(value):
    text = normalized_track_title(value)
    if not text:
        return ""
    return sanitize_track_stem(text).lower()


def cover_hint_tokens(value):
    tokens = re.split(r"[^a-z0-9]+", sanitized_name_key(value))
    tokens = [token for token in tokens if len(token) >= 3]
    return tokens


def cover_hint_matches(candidate_stem, hint):
    candidate_key = normalized_name_key(candidate_stem)
    hint_key = normalized_name_key(hint)
    if hint_key and (candidate_key == hint_key or candidate_key.startswith(f"{hint_key} [")):
        return True

    candidate_sanitized = sanitized_name_key(candidate_stem)
    hint_sanitized = sanitized_name_key(hint)
    if hint_sanitized and (
        candidate_sanitized == hint_sanitized
        or candidate_sanitized.startswith(hint_sanitized)
    ):
        return True
    return False


def find_cover_from_tokens(directory, files_in_dir, *hints):
    best_match = None
    best_score = 0
    for hint in hints:
        hint_tokens = cover_hint_tokens(hint)
        if len(hint_tokens) < 2:
            continue
        for candidate in sorted(files_in_dir):
            candidate_path = os.path.join(directory, candidate)
            if not os.path.isfile(candidate_path):
                continue
            candidate_stem, candidate_ext = os.path.splitext(candidate)
            if candidate_ext.lower() not in IMAGE_EXTENSIONS:
                continue
            candidate_tokens = set(cover_hint_tokens(candidate_stem))
            if hint_tokens and all(token in candidate_tokens for token in hint_tokens):
                score = len(hint_tokens)
                if score > best_score:
                    best_match = candidate
                    best_score = score
    return best_match


def find_cover_from_hints(directory, files_in_dir, *hints):
    for hint in hints:
        if not hint:
            continue
        for candidate in sorted(files_in_dir):
            candidate_path = os.path.join(directory, candidate)
            if not os.path.isfile(candidate_path):
                continue
            candidate_stem, candidate_ext = os.path.splitext(candidate)
            if candidate_ext.lower() not in IMAGE_EXTENSIONS:
                continue
            if cover_hint_matches(candidate_stem, hint):
                return candidate
    return None


def resolve_cover_rel_path(root, rel_path, title_hint=None, logical_hint=None):
    absolute_audio_path = os.path.join(root, rel_path)
    directory = os.path.dirname(absolute_audio_path)
    try:
        files_in_dir = os.listdir(directory)
    except FileNotFoundError:
        return None
    generic_cover = next(
        (candidate for candidate in files_in_dir if candidate.lower() in GENERIC_COVER_NAMES),
        None,
    )
    audio_base = os.path.splitext(os.path.basename(rel_path))[0]
    image_name = find_named_sibling(directory, audio_base, IMAGE_EXTENSIONS)
    if not image_name:
        image_name = find_cover_from_hints(directory, files_in_dir, title_hint, logical_hint)
    if not image_name:
        image_name = find_cover_from_tokens(directory, files_in_dir, logical_hint, title_hint, audio_base)
    if not image_name:
        image_name = generic_cover
    if not image_name:
        return None
    return os.path.join(os.path.dirname(rel_path), image_name).replace("\\", "/")


def public_cover_url(username, cover_rel_path):
    if not cover_rel_path:
        return None
    return url_for("public_cover", username=username, filename=cover_rel_path.replace("\\", "/"))


def dashboard_cover_url(username, cover_rel_path):
    if not cover_rel_path:
        return None
    return url_for("public_cover_preview", username=username, filename=cover_rel_path.replace("\\", "/"))


def build_public_absolute_url(relative_url):
    if not relative_url:
        return None
    if relative_url.startswith(("http://", "https://")):
        return relative_url
    if not PUBLIC_BASE_URL:
        return None
    if not relative_url.startswith("/"):
        relative_url = f"/{relative_url}"
    return f"{PUBLIC_BASE_URL}{relative_url}"


def get_user_track_count(user_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM tracks WHERE user_id = ?", (user_id,)).fetchone()
    return row["total"] if row else 0


def get_track_row(track_id):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT t.*, u.username AS owner_username
            FROM tracks t
            JOIN users u ON u.id = t.user_id
            WHERE t.id = ?
            """,
            (track_id,),
        ).fetchone()
    return row


def absolute_track_audio_path(row):
    return absolute_user_path(row["owner_username"], row["file_path"])


def absolute_track_cover_path(row):
    if not row["cover_path"]:
        return None
    return absolute_user_path(row["owner_username"], row["cover_path"])


def can_delete_track(user, row):
    return bool(user.is_admin or row["user_id"] == user.id)


def resolve_track_cover_path(row):
    cover_path = row["cover_path"]
    cover_ext = os.path.splitext(cover_path or "")[1].lower()
    if cover_path and cover_ext in IMAGE_EXTENSIONS:
        try:
            absolute_cover = absolute_user_path(row["owner_username"], cover_path)
        except PermissionError:
            absolute_cover = None
        if absolute_cover and os.path.exists(absolute_cover):
            return cover_path
    return resolve_cover_rel_path(
        user_audio_root(row["owner_username"]),
        row["file_path"],
        title_hint=row["title"],
        logical_hint=row["logical_name"],
    )


def normalized_track_title(value):
    return re.sub(r"\s+", " ", str(value or "")).strip(" .")


def ffmpeg_binary():
    if os.path.exists(FFMPEG_PATH):
        return FFMPEG_PATH
    return shutil.which("ffmpeg")


def cover_preview_cache_path(source_path):
    stat = os.stat(source_path)
    cache_key = hashlib.sha1(
        f"{source_path}|{stat.st_size}|{stat.st_mtime_ns}|{WEB_COVER_PREVIEW_SIZE}|{WEB_COVER_PREVIEW_QUALITY}".encode("utf-8")
    ).hexdigest()
    return os.path.join(WEB_COVER_CACHE_DIR, f"{cache_key}.webp")


def build_web_cover_preview(source_path):
    if not os.path.exists(source_path):
        return None

    ffmpeg_bin = ffmpeg_binary()
    if not ffmpeg_bin:
        return source_path

    ensure_directory(WEB_COVER_CACHE_DIR)
    preview_path = cover_preview_cache_path(source_path)
    if os.path.exists(preview_path) and os.path.getsize(preview_path) > 0:
        return preview_path

    temp_path = f"{preview_path}.tmp.webp"
    try:
        result = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                source_path,
                "-vf",
                f"scale='min({WEB_COVER_PREVIEW_SIZE},iw)':'min({WEB_COVER_PREVIEW_SIZE},ih)':force_original_aspect_ratio=decrease",
                "-frames:v",
                "1",
                "-c:v",
                "libwebp",
                "-q:v",
                str(WEB_COVER_PREVIEW_QUALITY),
                "-compression_level",
                "6",
                temp_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        result = None

    if not result or result.returncode != 0 or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return source_path

    os.replace(temp_path, preview_path)
    return preview_path


def cover_should_follow_track_identity(row, cover_rel_path):
    if not cover_rel_path:
        return False
    filename = os.path.basename(cover_rel_path).lower()
    if filename in GENERIC_COVER_NAMES:
        return False

    cover_stem = os.path.splitext(os.path.basename(cover_rel_path))[0]
    audio_stem = os.path.splitext(os.path.basename(row["file_path"]))[0]
    if normalized_name_key(cover_stem) == normalized_name_key(audio_stem):
        return True

    for hint in (row["title"], row["logical_name"]):
        if cover_hint_matches(cover_stem, hint):
            return True
    return False


def track_row_to_payload(row, viewer):
    try:
        absolute_audio = absolute_track_audio_path(row)
    except PermissionError:
        return None
    if not os.path.exists(absolute_audio):
        return None

    title = row["title"] or row["logical_name"]
    author = row["artist"] or row["owner_username"]
    rel_path = row["file_path"]
    cover_path = resolve_track_cover_path(row)
    return {
        "id": row["id"],
        "owner_id": row["user_id"],
        "owner": row["owner_username"],
        "requested_by": row["owner_username"],
        "rel_path": rel_path,
        "nom": title,
        "author": author,
        "category": category_from_rel_path(row["owner_username"], rel_path),
        "duration": row["duration"],
        "cover_url": dashboard_cover_url(row["owner_username"], cover_path),
        "full_cover_url": public_cover_url(row["owner_username"], cover_path),
        "cover_path": cover_path,
        "is_favorite": bool(row["is_favorite"]) if "is_favorite" in row.keys() else False,
        "can_delete": viewer.is_admin or row["user_id"] == viewer.id,
        "logical_name": row["logical_name"],
    }


def catalog_track_key(payload):
    title = re.sub(r"\s+", " ", str(payload.get("nom") or "")).strip().lower()
    author = re.sub(r"\s+", " ", str(payload.get("author") or "")).strip().lower()
    return title, author


def catalog_title_tokens(value):
    tokens = re.split(r"[^a-z0-9]+", sanitized_name_key(value))
    return {token for token in tokens if len(token) >= 3}


def tracks_look_related(payload_a, payload_b):
    if not payload_a or not payload_b:
        return False

    author_a = re.sub(r"\s+", " ", str(payload_a.get("author") or "")).strip().lower()
    author_b = re.sub(r"\s+", " ", str(payload_b.get("author") or "")).strip().lower()
    if not author_a or author_a != author_b:
        return False

    duration_a = payload_a.get("duration")
    duration_b = payload_b.get("duration")
    if duration_a is None or duration_b is None:
        return False
    if abs(float(duration_a) - float(duration_b)) > 1.5:
        return False

    tokens_a = catalog_title_tokens(payload_a.get("nom"))
    tokens_b = catalog_title_tokens(payload_b.get("nom"))
    if not tokens_a or not tokens_b:
        return False

    overlap = tokens_a & tokens_b
    if len(overlap) < 2:
        return False

    smaller_count = min(len(tokens_a), len(tokens_b))
    return len(overlap) >= max(2, smaller_count - 1)


def catalog_track_priority(payload, viewer):
    return (
        1 if payload.get("owner_id") == viewer.id else 0,
        1 if payload.get("is_favorite") else 0,
        1 if payload.get("cover_url") else 0,
        -int(payload.get("id") or 0),
    )


def fetch_visible_tracks(viewer):
    where_clause = ""
    query_params = [viewer.id]
    if not viewer.is_admin:
        where_clause = "WHERE t.user_id = ?"
        query_params.append(viewer.id)

    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT t.*, u.username AS owner_username,
                   CASE WHEN f.user_id IS NULL THEN 0 ELSE 1 END AS is_favorite
            FROM tracks t
            JOIN users u ON u.id = t.user_id
            LEFT JOIN favorites f ON f.track_id = t.id AND f.user_id = ?
            {where_clause}
            ORDER BY LOWER(COALESCE(t.title, t.logical_name)) ASC, t.id ASC
            """,
            query_params,
        ).fetchall()

    deduped_payloads = {}
    transient_missing_track_ids = []
    for row in rows:
        payload = track_row_to_payload(row, viewer)
        if payload is None:
            transient_missing_track_ids.append(row["id"])
            continue

        key = catalog_track_key(payload)
        chosen_key = key
        current = deduped_payloads.get(chosen_key)

        if current is None:
            for existing_key, existing_payload in deduped_payloads.items():
                if tracks_look_related(existing_payload, payload):
                    chosen_key = existing_key
                    current = existing_payload
                    break

        if current is None or catalog_track_priority(payload, viewer) > catalog_track_priority(current, viewer):
            deduped_payloads[chosen_key] = payload

    if transient_missing_track_ids:
        # A file can be temporarily absent from its old path while a rename is in flight.
        # Let the periodic library sync reconcile true deletions instead of dropping rows here.
        print(f"[WARN] Tracks temporairement indisponibles ignores: {len(transient_missing_track_ids)}")

    return sorted(
        deduped_payloads.values(),
        key=lambda payload: (str(payload.get("nom") or "").lower(), int(payload.get("id") or 0)),
    )


def fetch_playlists_payload(viewer):
    with get_db_connection() as conn:
        playlist_rows = conn.execute(
            """
            SELECT p.id, p.name
            FROM playlists p
            WHERE p.user_id = ? AND (p.url IS NULL OR TRIM(p.url) = '')
            ORDER BY LOWER(p.name), p.id
            """,
            (viewer.id,),
        ).fetchall()
        track_rows = conn.execute(
            """
            SELECT p.id AS playlist_id, p.name AS playlist_name, pt.position,
                   t.*, u.username AS owner_username,
                   CASE WHEN f.user_id IS NULL THEN 0 ELSE 1 END AS is_favorite
            FROM playlists p
            LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
            LEFT JOIN tracks t ON t.id = pt.track_id
            LEFT JOIN users u ON u.id = t.user_id
            LEFT JOIN favorites f ON f.track_id = t.id AND f.user_id = ?
            WHERE p.user_id = ? AND (p.url IS NULL OR TRIM(p.url) = '')
            ORDER BY LOWER(p.name), p.id, pt.position, pt.rowid
            """,
            (viewer.id, viewer.id),
        ).fetchall()

    playlists = []
    playlist_map = {}
    for row in playlist_rows:
        item = {"id": row["id"], "name": row["name"], "tracks": []}
        playlists.append(item)
        playlist_map[row["id"]] = item

    for row in track_rows:
        if row["playlist_id"] not in playlist_map or row["id"] is None:
            continue
        payload = track_row_to_payload(row, viewer)
        if payload is None:
            continue
        playlist_map[row["playlist_id"]]["tracks"].append(payload)

    return playlists


def build_dashboard_payload(viewer):
    sync_user_library_if_stale(viewer.id, viewer.username)
    tracks = fetch_visible_tracks(viewer)
    track_count = get_user_track_count(viewer.id)
    return {
        "tracks": tracks,
        "favorites": [track for track in tracks if track["is_favorite"]],
        "playlists": fetch_playlists_payload(viewer),
        "player_state": jukebox.snapshot(viewer),
        "track_count": track_count,
        "track_limit": MAX_TRACKS_PER_USER,
        "remaining_slots": max(0, MAX_TRACKS_PER_USER - track_count),
    }


def mark_user_library_synced(user_id):
    with library_sync_lock:
        library_sync_timestamps[user_id] = time.monotonic()
        library_sync_inflight.discard(user_id)


def mark_user_library_stale(user_id):
    with library_sync_lock:
        library_sync_timestamps.pop(user_id, None)
        library_sync_inflight.discard(user_id)


def sync_user_library_if_stale(user_id, username, min_interval_seconds=LIBRARY_SYNC_MIN_INTERVAL):
    now = time.monotonic()
    with library_sync_lock:
        last_sync = library_sync_timestamps.get(user_id)
        if user_id in library_sync_inflight:
            return False
        if last_sync is not None and now - last_sync < min_interval_seconds:
            return False
        library_sync_inflight.add(user_id)

    success = False
    try:
        sync_user_library(user_id, username)
        success = True
        return True
    finally:
        if not success:
            mark_user_library_stale(user_id)


def sync_user_library(user_id, username):
    root = user_audio_root(username)
    ensure_directory(root)

    with get_db_connection() as conn:
        existing_rows = conn.execute(
            """
            SELECT id, file_path, logical_name, title, artist, cover_path, duration
            FROM tracks
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
    existing_by_path = {row["file_path"]: row for row in existing_rows}

    discovered = {}
    seen_logical_names = set()
    for current_root, _, files in os.walk(root):
        for filename in sorted(files):
            if not filename.lower().endswith(EXTENSIONS_AUDIO):
                continue
            if filename.endswith(".part") or filename.endswith(".ytdl"):
                continue
            absolute_path = os.path.join(current_root, filename)
            rel_path = os.path.relpath(absolute_path, root).replace("\\", "/")
            logical_name = sanitize_track_stem(filename)
            logical_name_key = logical_name.lower()
            if logical_name_key in seen_logical_names:
                print(f"[WARN] Morceau en doublon ignore pour {username}: {rel_path}")
                continue
            seen_logical_names.add(logical_name_key)
            existing = existing_by_path.get(rel_path)
            needs_metadata = existing is None or not existing["title"] or not existing["artist"] or existing["duration"] is None
            metadata = read_audio_metadata(absolute_path) if needs_metadata else {}
            title = (existing["title"] if existing else None) or metadata.get("title") or logical_name
            artist = (existing["artist"] if existing else None) or metadata.get("author") or category_from_rel_path(username, rel_path)
            cover_path = resolve_cover_rel_path(
                root,
                rel_path,
                title_hint=title,
                logical_hint=(existing["logical_name"] if existing and existing["logical_name"] else logical_name),
            )
            if (
                existing
                and not existing["cover_path"]
                and cover_path
                and os.path.basename(cover_path).lower() not in GENERIC_COVER_NAMES
                and normalized_name_key(existing["title"]) != normalized_name_key(logical_name)
                and normalized_name_key(os.path.splitext(os.path.basename(cover_path))[0]) != normalized_name_key(logical_name)
            ):
                title = logical_name
            discovered[rel_path] = {
                "logical_name": logical_name,
                "title": title,
                "artist": artist,
                "file_path": rel_path,
                "cover_path": cover_path,
                "duration": existing["duration"] if existing and existing["duration"] is not None else metadata.get("duration"),
            }

    with get_db_connection() as conn:
        for rel_path, payload in discovered.items():
            if rel_path in existing_by_path:
                conn.execute(
                    """
                    UPDATE tracks
                    SET logical_name = ?, title = ?, artist = ?, cover_path = ?, duration = ?
                    WHERE user_id = ? AND file_path = ?
                    """,
                    (
                        payload["logical_name"],
                        payload["title"],
                        payload["artist"],
                        payload["cover_path"],
                        payload["duration"],
                        user_id,
                        rel_path,
                    ),
                )
            else:
                try:
                    conn.execute(
                        """
                        INSERT INTO tracks (user_id, logical_name, title, artist, file_path, cover_path, duration)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            payload["logical_name"],
                            payload["title"],
                            payload["artist"],
                            payload["file_path"],
                            payload["cover_path"],
                            payload["duration"],
                        ),
                    )
                except sqlite3.IntegrityError:
                    print(f"[WARN] Index DB ignore un doublon pour {username}: {rel_path}")

        discovered_paths = set(discovered.keys())
        for row in existing_rows:
            if row["file_path"] not in discovered_paths:
                conn.execute("DELETE FROM tracks WHERE id = ?", (row["id"],))
        conn.commit()
    mark_user_library_synced(user_id)


def sync_library_database():
    with get_db_connection() as conn:
        users = conn.execute("SELECT id, username FROM users").fetchall()
    for row in users:
        sync_user_library(row["id"], row["username"])


def safe_username_fragment(username):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", username).strip("_") or "member"


def user_audio_root(username):
    return os.path.normpath(os.path.join(AUDIO_ROOT, username))


def absolute_user_path(username, rel_path):
    root = user_audio_root(username)
    candidate = os.path.normpath(os.path.join(root, rel_path))
    if not os.path.abspath(candidate).startswith(os.path.abspath(root)):
        raise PermissionError("Acces refuse")
    return candidate


def resolve_discord_thumbnail(track_info):
    thumbnail_url = build_public_absolute_url(track_info.get("public_thumbnail_url") or track_info.get("cover_url"))
    if thumbnail_url:
        return {"url": thumbnail_url, "attachment_path": None}

    thumbnail_path = track_info.get("thumbnail_path")
    owner = track_info.get("owner")
    if not thumbnail_path or not owner:
        return {"url": None, "attachment_path": None}

    try:
        absolute_path = absolute_user_path(owner, thumbnail_path)
    except PermissionError:
        return {"url": None, "attachment_path": None}

    if not os.path.exists(absolute_path):
        return {"url": None, "attachment_path": None}

    return {"url": None, "attachment_path": absolute_path}


def encode_multipart_formdata(fields, files):
    boundary = f"----NixifyBoundary{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for name, file_info in files.items():
        filename = file_info["filename"]
        content_type = file_info.get("content_type") or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(file_info["content"])
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), boundary


def send_discord_notification(track_info):
    if not DISCORD_WEBHOOK_URL:
        return

    title = (track_info.get("nom") or "Titre inconnu").strip()
    author = (track_info.get("author") or track_info.get("owner") or "Auteur inconnu").strip()
    requested_by = (track_info.get("requested_by") or track_info.get("owner") or "Inconnu").strip()

    try:
        thumbnail_data = resolve_discord_thumbnail(track_info)
        embed = {
            "title": "🎶 En cours de lecture sur les enceintes",
            "description": f"**{title}** par **{author}**",
            "color": DISCORD_EMBED_COLOR,
            "fields": [
                {
                    "name": "Proposé par",
                    "value": requested_by,
                    "inline": False,
                }
            ],
            "footer": {"text": DISCORD_FOOTER_TEXT},
        }

        files = {}
        if thumbnail_data["url"]:
            embed["thumbnail"] = {"url": thumbnail_data["url"]}
        elif thumbnail_data["attachment_path"]:
            attachment_path = thumbnail_data["attachment_path"]
            filename = os.path.basename(attachment_path)
            with open(attachment_path, "rb") as handle:
                files["files[0]"] = {
                    "filename": filename,
                    "content": handle.read(),
                    "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                }
            embed["thumbnail"] = {"url": f"attachment://{filename}"}

        payload = {"embeds": [embed]}

        if files:
            body, boundary = encode_multipart_formdata(
                {"payload_json": json.dumps(payload, ensure_ascii=False)},
                files,
            )
            request_obj = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": DISCORD_HTTP_USER_AGENT,
                    "Accept": "application/json",
                },
                method="POST",
            )
        else:
            request_obj = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": DISCORD_HTTP_USER_AGENT,
                    "Accept": "application/json",
                },
                method="POST",
            )

        with urllib.request.urlopen(request_obj, timeout=8) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            details = ""
        suffix = f" | {details}" if details else ""
        print(f"[WARN] Notification Discord impossible pour {title}: {exc}{suffix}")
    except (OSError, urllib.error.URLError) as exc:
        print(f"[WARN] Notification Discord impossible pour {title}: {exc}")


def detect_cover(root, rel_path, files_in_dir, clean_name, generic_cover):
    audio_filename = os.path.basename(rel_path)
    audio_base = os.path.splitext(audio_filename)[0]
    image_name = find_named_sibling(os.path.dirname(os.path.join(root, rel_path)), audio_base, IMAGE_EXTENSIONS)
    if not image_name and clean_name != audio_base:
        image_name = find_named_sibling(os.path.dirname(os.path.join(root, rel_path)), clean_name, IMAGE_EXTENSIONS)
    if not image_name:
        image_name = generic_cover
    if not image_name:
        return None
    relative_dir = os.path.dirname(rel_path)
    relative_cover = os.path.join(relative_dir, image_name).replace("\\", "/")
    return url_for("get_cover", username=os.path.basename(root), filename=relative_cover)


def read_sidecar_metadata(absolute_audio_path):
    base_path = os.path.splitext(absolute_audio_path)[0]
    info_json_path = f"{base_path}.info.json"
    if not os.path.exists(info_json_path):
        return {}
    try:
        with open(info_json_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}

    return {
        "title": data.get("track") or data.get("title"),
        "author": data.get("artist") or data.get("uploader") or data.get("channel"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
    }


def sanitize_track_stem(filename):
    raw_stem = Path(filename).stem.strip()
    cleaned = re.sub(r"[^A-Za-z0-9 _-]+", "_", raw_stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
    return cleaned or "morceau"


def probe_audio_metadata(absolute_audio_path):
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return {}
    try:
        result = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                absolute_audio_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout or "{}")
    except Exception:
        return {}

    format_data = data.get("format") or {}
    format_tags = format_data.get("tags") or {}
    audio_stream = None
    artwork_stream = None
    for stream in data.get("streams") or []:
        codec_type = (stream.get("codec_type") or "").lower()
        disposition = stream.get("disposition") or {}
        if codec_type == "audio" and audio_stream is None:
            audio_stream = stream
        if codec_type == "video" and (disposition.get("attached_pic") == 1 or disposition.get("attached_pic") is True):
            artwork_stream = stream

    stream_tags = (audio_stream or {}).get("tags") or {}
    tags = {**format_tags, **stream_tags}
    title = tags.get("title") or tags.get("TITLE")
    author = (
        tags.get("artist")
        or tags.get("ARTIST")
        or tags.get("album_artist")
        or tags.get("ALBUM_ARTIST")
        or tags.get("composer")
        or tags.get("COMPOSER")
    )
    duration = format_data.get("duration") or (audio_stream or {}).get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except Exception:
        duration = None

    return {
        "title": title,
        "author": author,
        "duration": duration,
        "has_embedded_cover": artwork_stream is not None,
    }


def read_audio_metadata(absolute_audio_path):
    sidecar = read_sidecar_metadata(absolute_audio_path)
    probe = probe_audio_metadata(absolute_audio_path)
    merged = {}
    for key in ("title", "author", "duration", "thumbnail", "has_embedded_cover"):
        value = sidecar.get(key)
        if value in (None, "", []):
            value = probe.get(key)
        merged[key] = value
    return merged


def find_named_sibling(directory, stem, extensions):
    stem_lower = stem.lower()
    try:
        candidates = os.listdir(directory)
    except FileNotFoundError:
        return None
    for candidate in sorted(candidates):
        candidate_path = os.path.join(directory, candidate)
        if not os.path.isfile(candidate_path):
            continue
        candidate_stem, candidate_ext = os.path.splitext(candidate)
        if candidate_stem.lower() == stem_lower and candidate_ext.lower() in extensions:
            return candidate
    return None


def find_library_conflict(root, stem, extensions):
    stem_lower = stem.lower()
    for current_root, _, files in os.walk(root):
        for filename in files:
            candidate_stem, candidate_ext = os.path.splitext(filename)
            if candidate_stem.lower() == stem_lower and candidate_ext.lower() in extensions:
                return os.path.join(current_root, filename)
    return None


def extract_embedded_cover(absolute_audio_path, output_base_path):
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None
    output_path = f"{output_base_path}.jpg"
    try:
        result = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                absolute_audio_path,
                "-an",
                "-map",
                "0:v:0",
                "-c:v",
                "mjpeg",
                output_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        return None
    return output_path


def uploaded_file_size(uploaded_file):
    uploaded_file.stream.seek(0, os.SEEK_END)
    size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0)
    return size


def classify_uploaded_extension(filename):
    extension = os.path.splitext(filename)[1].lower()
    if extension in EXTENSIONS_AUDIO:
        return "audio", extension
    if extension in IMAGE_EXTENSIONS:
        return "image", extension
    if extension in ALLOWED_COOKIE_EXTENSIONS:
        return "cookie", extension
    return "other", extension


def store_uploaded_track(user, audio_file, image_file=None, origin="upload"):
    def reject(reason):
        print(f"[{origin.upper()}][SKIP] {user.username}: {reason}")
        return False, reason, None

    if not audio_file or not audio_file.filename:
        return reject("Aucun morceau audio selectionne.")

    audio_kind, audio_ext = classify_uploaded_extension(audio_file.filename)
    if audio_kind != "audio":
        return reject(f"Le morceau doit etre un fichier audio supporte ({audio_file.filename}).")

    audio_size = uploaded_file_size(audio_file)
    if audio_size <= 0:
        return reject(f"Le morceau audio est vide ({audio_file.filename}).")
    if audio_size > MAX_TRACK_FILESIZE:
        return reject(f"Le morceau depasse la limite de {MAX_TRACK_FILESIZE_LABEL} ({audio_file.filename}, {audio_size} bytes).")
    if get_user_track_count(user.id) >= MAX_TRACKS_PER_USER:
        return reject(f"Limite atteinte: {MAX_TRACKS_PER_USER} morceaux maximum par utilisateur.")

    image_ext = ""
    if image_file and image_file.filename:
        image_kind, image_ext = classify_uploaded_extension(image_file.filename)
        if image_kind != "image":
            return reject(f"L'image doit etre au format JPG, PNG ou WEBP ({image_file.filename}).")
        image_size = uploaded_file_size(image_file)
        if image_size <= 0:
            return reject(f"L'image envoyee est vide ({image_file.filename}).")
        if image_size > MAX_IMAGE_UPLOAD_BYTES:
            return reject(f"L'image depasse la limite de 10 Mo ({image_file.filename}, {image_size} bytes).")

    destination_root = user_audio_root(user.username)
    ensure_directory(destination_root)

    track_stem = sanitize_track_stem(os.path.basename(audio_file.filename))
    audio_conflict = find_library_conflict(destination_root, track_stem, EXTENSIONS_AUDIO)
    if audio_conflict:
        return reject(f"Un autre morceau utilise deja ce nom ({track_stem}).")

    image_conflict = image_file and find_library_conflict(destination_root, track_stem, IMAGE_EXTENSIONS)
    if image_conflict:
        return reject(f"Une image existe deja pour ce nom de morceau ({track_stem}).")

    audio_destination = os.path.join(destination_root, secure_filename(f"{track_stem}{audio_ext}"))
    image_destination = os.path.join(destination_root, secure_filename(f"{track_stem}{image_ext}")) if image_file and image_ext else None
    extracted_cover_path = None

    try:
        audio_file.save(audio_destination)
        if image_file and image_destination:
            image_file.save(image_destination)
        else:
            extracted_cover_path = extract_embedded_cover(audio_destination, os.path.join(destination_root, track_stem))
    except Exception:
        for candidate in (audio_destination, image_destination, extracted_cover_path):
            if candidate and os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass
        return reject(f"L'enregistrement du morceau a echoue ({audio_file.filename}).")

    metadata = read_audio_metadata(audio_destination)
    rel_audio_path = os.path.relpath(audio_destination, destination_root).replace("\\", "/")
    rel_cover_path = resolve_cover_rel_path(destination_root, rel_audio_path, title_hint=metadata.get("title"), logical_hint=track_stem)
    title = metadata.get("title") or track_stem
    artist = metadata.get("author") or category_from_rel_path(user.username, rel_audio_path)

    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO tracks (user_id, logical_name, title, artist, file_path, cover_path, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    track_stem,
                    title,
                    artist,
                    rel_audio_path,
                    rel_cover_path,
                    metadata.get("duration"),
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        for candidate in (audio_destination, image_destination, extracted_cover_path):
            if candidate and os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass
        return reject(f"Ce nom de morceau existe deja dans votre bibliotheque ({track_stem}).")

    details = []
    if image_file and image_destination:
        details.append(f"image associee : {os.path.basename(image_destination)}")
    elif extracted_cover_path:
        details.append(f"miniature extraite : {os.path.basename(extracted_cover_path)}")
    elif metadata.get("has_embedded_cover"):
        details.append("miniature detectee mais extraction impossible")
    print(
        f"[{origin.upper()}][OK] {user.username}: {audio_destination} ({audio_size} bytes)"
        + (f" | {' | '.join(details)}" if details else "")
    )
    mark_user_library_synced(user.id)
    return True, "Morceau ajoute a votre bibliotheque.", {"stem": track_stem, "audio_path": audio_destination, "details": details}


def scan_user_tracks(username):
    tracks = []
    root = user_audio_root(username)
    if not os.path.exists(root):
        return tracks

    for current_root, _, files in os.walk(root):
        files_in_dir = list(files)
        generic_cover = next(
            (candidate for candidate in files_in_dir if candidate.lower() in GENERIC_COVER_NAMES),
            None,
        )
        for filename in sorted(files):
            if not filename.lower().endswith(EXTENSIONS_AUDIO):
                continue
            absolute_path = os.path.join(current_root, filename)
            if filename.endswith(".part") or filename.endswith(".ytdl"):
                continue

            rel_path = os.path.relpath(absolute_path, root).replace("\\", "/")
            display_name = filename.rsplit(" [", 1)[0] if " [" in filename else os.path.splitext(filename)[0]
            folder_name = os.path.basename(current_root)
            category = "Bibliotheque" if folder_name == username else folder_name
            metadata = read_audio_metadata(absolute_path)
            tracks.append(
                {
                    "rel_path": rel_path,
                    "nom": metadata.get("title") or display_name,
                    "author": metadata.get("author") or category,
                    "category": category,
                    "duration": metadata.get("duration"),
                    "cover_url": detect_cover(root, rel_path, files_in_dir, display_name, generic_cover),
                }
            )
    return tracks


def _candidate_ipv4s():
    values = []

    configured = os.environ.get("NIXIFY_HOST_IP", "").strip()
    if configured:
        values.append(configured)

    for target in (("1.1.1.1", 80), ("8.8.8.8", 80)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(target)
            values.append(sock.getsockname()[0])
        except Exception:
            pass
        finally:
            sock.close()

    try:
        hostname_ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        hostname_ips = []
    values.extend(hostname_ips)

    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            values.extend(result.stdout.split())
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)\b", result.stdout)
            if match:
                values.append(match.group(1))
    except Exception:
        pass

    return values


def _score_ipv4_address(ip_address):
    if not ip_address or ip_address.startswith("127."):
        return -1
    if ip_address.startswith(("10.", "192.168.")):
        return 4
    if ip_address.startswith("172."):
        try:
            second_octet = int(ip_address.split(".")[1])
        except Exception:
            second_octet = -1
        if 16 <= second_octet <= 31:
            return 4
    if ip_address.startswith("100."):
        return 2
    return 1


def get_local_ip():
    global cached_local_ip, cached_local_ip_score

    with local_ip_lock:
        if cached_local_ip and cached_local_ip_score >= 2:
            return cached_local_ip

        best_ip = None
        best_score = -1
        for candidate in _candidate_ipv4s():
            try:
                socket.inet_aton(candidate)
            except OSError:
                continue
            score = _score_ipv4_address(candidate)
            if score > best_score:
                best_ip = candidate
                best_score = score

        if best_ip and best_score >= 0:
            cached_local_ip = best_ip
            cached_local_ip_score = best_score
        else:
            cached_local_ip = None
            cached_local_ip_score = -1

        if cached_local_ip:
            return cached_local_ip

        # Do not permanently cache loopback fallback: retry on later requests.
        return "127.0.0.1"


def get_display_host():
    configured = os.environ.get("NIXIFY_DISPLAY_HOST", "").strip()
    if configured:
        return configured

    public_base = PUBLIC_BASE_URL.strip()
    if public_base:
        host = public_base.split("://", 1)[-1].split("/", 1)[0].strip()
        if host:
            return host

    return get_local_ip()


class PlaybackBackend:
    name = "none"
    supports_web_volume = False

    def play(self, file_path):
        raise NotImplementedError

    def wait_until_end(self, stop_event):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def get_time_ms(self):
        return None

    def get_length_ms(self):
        return None

    def set_volume(self, percent):
        return False

    def get_volume(self):
        return None


class VLCPlaybackBackend(PlaybackBackend):
    name = "python-vlc"
    supports_web_volume = True

    def __init__(self):
        self.instance = vlc.Instance("--no-video") if vlc else None
        self.player = self.instance.media_player_new() if self.instance else None

    def play(self, file_path):
        if not self.player or not self.instance:
            return False, "python-vlc indisponible"
        media = self.instance.media_new(file_path)
        self.player.set_media(media)
        result = self.player.play()
        if result == -1:
            return False, "Lecture VLC impossible"
        time.sleep(0.2)
        return True, None

    def set_volume(self, percent):
        if not self.player:
            return False
        try:
            return self.player.audio_set_volume(int(percent)) != -1
        except Exception:
            return False

    def get_volume(self):
        if not self.player:
            return None
        try:
            value = self.player.audio_get_volume()
        except Exception:
            return None
        return value if value is not None and value >= 0 else None

    def wait_until_end(self, stop_event):
        if not self.player:
            return "error"
        while True:
            if stop_event.is_set():
                self.stop()
                return "stopped"
            state = self.player.get_state()
            if state in {vlc.State.Ended, vlc.State.Stopped}:
                return "ended"
            if state in {vlc.State.Error}:
                return "error"
            time.sleep(0.25)

    def stop(self):
        if self.player:
            self.player.stop()

    def get_time_ms(self):
        if not self.player:
            return None
        value = self.player.get_time()
        return value if value and value >= 0 else None

    def get_length_ms(self):
        if not self.player:
            return None
        value = self.player.get_length()
        return value if value and value >= 0 else None


class CommandPlaybackBackend(PlaybackBackend):
    def __init__(self, command):
        self.command = command
        self.name = os.path.basename(command[0])
        self.process = None

    def play(self, file_path):
        try:
            self.process = subprocess.Popen(self.command + [file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def wait_until_end(self, stop_event):
        if not self.process:
            return "error"
        while True:
            if stop_event.is_set():
                self.stop()
                return "stopped"
            code = self.process.poll()
            if code is not None:
                return "ended" if code == 0 else "error"
            time.sleep(0.25)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


def build_playback_backend():
    if vlc:
        try:
            return VLCPlaybackBackend()
        except Exception:
            # libvlc peut manquer meme si le module python-vlc est installe.
            pass

    candidates = [
        ["mpv", "--no-video", "--really-quiet", "--force-window=no"],
        ["mplayer", "-really-quiet", "-novideo"],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ]
    for command in candidates:
        if shutil.which(command[0]):
            return CommandPlaybackBackend(command)
    return None


class JukeboxPlayer:
    def __init__(self):
        self.backend = build_playback_backend()
        self.condition = threading.Condition()
        self.user_queues = {}
        self.turn_order = deque()
        self.priority_queue = deque()
        self.current = None
        self.last_error = None
        self.volume = DEFAULT_PLAYBACK_VOLUME
        self.stop_event = threading.Event()
        self.deleted_track_ids = set()
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    @property
    def backend_name(self):
        return self.backend.name if self.backend else "indisponible"

    @property
    def queue_limit(self):
        return MAX_SHARED_QUEUE_SIZE

    def _apply_volume_locked(self):
        if not self.backend:
            return False
        applied = self.backend.set_volume(self.volume)
        backend_value = self.backend.get_volume()
        if backend_value is not None:
            self.volume = max(0, min(100, int(backend_value)))
        return applied

    def set_volume(self, percent):
        try:
            target = int(round(float(percent)))
        except (TypeError, ValueError):
            return False, "Volume invalide."
        target = max(0, min(100, target))
        with self.condition:
            self.volume = target
            applied = self._apply_volume_locked()
            self.condition.notify_all()
        if applied or not self.backend:
            return True, None
        return False, f"Le backend {self.backend_name} ne permet pas de regler le volume depuis le web."

    def enqueue(self, track, play_now=False, play_next=False):
        return self.enqueue_many([track], play_now=play_now, play_next=play_next)

    def enqueue_many(self, tracks, play_now=False, play_next=False):
        if not tracks:
            return False, "Aucun morceau a ajouter."
        with self.condition:
            if play_now:
                self.priority_queue.appendleft(dict(tracks[0]))
                if self.current:
                    self.stop_event.set()
                    if self.backend:
                        self.backend.stop()
                self.condition.notify_all()
                return True, None
            if self._queued_total_locked() + len(tracks) > self.queue_limit:
                return False, f"File d'attente pleine ({self.queue_limit} morceaux max)."
            if play_next:
                for track in tracks:
                    self.priority_queue.append(dict(track))
                self.condition.notify_all()
                return True, None
            for track in tracks:
                self._append_track_locked(track)
            self.condition.notify_all()
            return True, None

    def _queued_total_locked(self):
        return len(self.priority_queue) + sum(len(queue) for queue in self.user_queues.values())

    def _append_track_locked(self, track):
        requester = track["requested_by"]
        queue = self.user_queues.setdefault(requester, deque())
        queue.append(dict(track))
        if requester not in self.turn_order:
            self.turn_order.append(requester)

    def _dequeue_next_locked(self):
        if self.priority_queue:
            return dict(self.priority_queue.popleft())

        while self.turn_order:
            requester = self.turn_order.popleft()
            queue = self.user_queues.get(requester)
            if not queue:
                continue
            track = queue.popleft()
            if queue:
                self.turn_order.append(requester)
            else:
                self.user_queues.pop(requester, None)
            return dict(track)
        return None

    def _cleanup_turn_order_locked(self):
        active_users = {user for user, queue in self.user_queues.items() if queue}
        self.turn_order = deque([user for user in self.turn_order if user in active_users])

    def _requeue_loop_locked(self, track):
        if not track.get("loop_mode"):
            return
        if track.get("track_id") in self.deleted_track_ids:
            return
        if not os.path.exists(track["abs_path"]):
            return
        self._append_track_locked(track)

    def skip(self):
        with self.condition:
            if not self.current:
                return False
            self.stop_event.set()
            if self.backend:
                self.backend.stop()
            self.condition.notify_all()
            return True

    def purge_matching(self, predicate):
        with self.condition:
            removed = 0

            if self.priority_queue:
                filtered_priority = deque()
                for item in self.priority_queue:
                    if predicate(item):
                        removed += 1
                    else:
                        filtered_priority.append(item)
                self.priority_queue = filtered_priority

            for requester in list(self.user_queues.keys()):
                queue = self.user_queues.get(requester) or deque()
                filtered = deque()
                for item in queue:
                    if predicate(item):
                        removed += 1
                    else:
                        filtered.append(item)
                if filtered:
                    self.user_queues[requester] = filtered
                else:
                    self.user_queues.pop(requester, None)

            stop_current = bool(self.current and predicate(self.current))
            self._cleanup_turn_order_locked()

            if stop_current:
                self.stop_event.set()
                if self.backend:
                    self.backend.stop()
                removed += 1

            if removed:
                self.condition.notify_all()
            return removed

    def clear_user_requests(self, username):
        return self.purge_matching(lambda item: item.get("requested_by") == username)

    def _queue_preview_entries_locked(self):
        preview = []

        for source_index, item in enumerate(self.priority_queue):
            preview.append(
                {
                    "track": dict(item),
                    "is_priority": True,
                    "source": "priority",
                    "source_index": source_index,
                    "requester": item.get("requested_by"),
                }
            )

        shadow_queues = {
            user: deque((index, dict(item)) for index, item in enumerate(queue))
            for user, queue in self.user_queues.items()
            if queue
        }
        shadow_order = deque([user for user in self.turn_order if user in shadow_queues])

        while shadow_order:
            requester = shadow_order.popleft()
            queue = shadow_queues.get(requester)
            if not queue:
                continue
            source_index, track = queue.popleft()
            preview.append(
                {
                    "track": track,
                    "is_priority": False,
                    "source": "user",
                    "source_index": source_index,
                    "requester": requester,
                }
            )
            if queue:
                shadow_order.append(requester)

        return preview

    def _viewer_can_skip_locked(self, viewer, preview_entries=None):
        if not viewer or viewer.is_admin:
            return bool(viewer and viewer.is_admin)
        if self.current and normalize_username(self.current.get("requested_by")) == viewer.normalized_username:
            return True
        preview_entries = preview_entries if preview_entries is not None else self._queue_preview_entries_locked()
        if preview_entries:
            next_track = preview_entries[0]["track"]
            return normalize_username(next_track.get("requested_by")) == viewer.normalized_username
        return False

    def remove_queue_item(self, queue_index, viewer):
        with self.condition:
            preview_entries = self._queue_preview_entries_locked()
            if queue_index < 0 or queue_index >= len(preview_entries):
                return False, "Morceau introuvable dans la file."

            entry = preview_entries[queue_index]
            track = entry["track"]
            if not viewer.is_admin and normalize_username(track.get("requested_by")) != viewer.normalized_username:
                return False, "Retrait interdit pour ce morceau."

            removed = False
            if entry["source"] == "priority":
                filtered_priority = deque(
                    item for index, item in enumerate(self.priority_queue) if index != entry["source_index"]
                )
                removed = len(filtered_priority) != len(self.priority_queue)
                self.priority_queue = filtered_priority
            else:
                requester = entry["requester"]
                queue = self.user_queues.get(requester) or deque()
                filtered = deque(item for index, item in enumerate(queue) if index != entry["source_index"])
                removed = len(filtered) != len(queue)
                if filtered:
                    self.user_queues[requester] = filtered
                else:
                    self.user_queues.pop(requester, None)
                self._cleanup_turn_order_locked()

            if removed:
                self.condition.notify_all()
                return True, "Morceau retire de la file."
            return False, "Morceau introuvable dans la file."

    def remove_track(self, track_id):
        with self.condition:
            removed = False
            self.deleted_track_ids.add(track_id)
            if self.priority_queue:
                filtered_priority = deque(item for item in self.priority_queue if item.get("track_id") != track_id)
                if len(filtered_priority) != len(self.priority_queue):
                    self.priority_queue = filtered_priority
                    removed = True

            for requester in list(self.user_queues.keys()):
                queue = self.user_queues.get(requester) or deque()
                filtered = deque(item for item in queue if item.get("track_id") != track_id)
                if len(filtered) != len(queue):
                    removed = True
                if filtered:
                    self.user_queues[requester] = filtered
                else:
                    self.user_queues.pop(requester, None)

            self._cleanup_turn_order_locked()

            if self.current and self.current.get("track_id") == track_id:
                self.stop_event.set()
                if self.backend:
                    self.backend.stop()
                removed = True

            if removed:
                self.condition.notify_all()
            return removed

    def track_is_reserved(self, track_id):
        with self.condition:
            if self.current and self.current.get("track_id") == track_id:
                return True
            if any(item.get("track_id") == track_id for item in self.priority_queue):
                return True
            return any(
                item.get("track_id") == track_id
                for queue in self.user_queues.values()
                for item in queue
            )

    def _queue_preview_locked(self):
        return [{**entry["track"], "is_priority": entry["is_priority"]} for entry in self._queue_preview_entries_locked()]

    def _notify_track_started(self, track):
        threading.Thread(target=send_discord_notification, args=(dict(track),), daemon=True).start()

    def _loop(self):
        while True:
            with self.condition:
                while self._queued_total_locked() == 0:
                    self.current = None
                    self.condition.wait()
                track = self._dequeue_next_locked()
                if not track:
                    continue
                self.current = {**track, "started_at": datetime.datetime.utcnow().isoformat() + "Z"}
                self.stop_event.clear()

            if not self.backend:
                self.last_error = "Aucun backend audio detecte. Installe python-vlc, mpv, mplayer ou ffplay."
                time.sleep(1)
                with self.condition:
                    self.current = None
                continue

            ok, error = self.backend.play(track["abs_path"])
            if not ok:
                self.last_error = error
                with self.condition:
                    self.current = None
                continue
            with self.condition:
                self._apply_volume_locked()

            self._notify_track_started(self.current or track)

            result = self.backend.wait_until_end(self.stop_event)
            if result == "error":
                self.last_error = f"Lecture interrompue sur {track['nom']}"
            elif result == "ended":
                self.last_error = None

            with self.condition:
                if result == "ended":
                    self._requeue_loop_locked(track)
                self.current = None
                self.stop_event.clear()

    def snapshot(self, viewer=None):
        with self.condition:
            current = dict(self.current) if self.current else None
            preview_entries = self._queue_preview_entries_locked()
            queue = [
                {
                    "track_id": entry["track"].get("track_id"),
                    "nom": entry["track"]["nom"],
                    "author": entry["track"].get("author"),
                    "owner": entry["track"]["owner"],
                    "category": entry["track"]["category"],
                    "requested_by": entry["track"]["requested_by"],
                    "cover_url": entry["track"].get("cover_url"),
                    "public_thumbnail_url": entry["track"].get("public_thumbnail_url"),
                    "thumbnail_path": entry["track"].get("thumbnail_path"),
                    "duration": entry["track"].get("duration"),
                    "loop_mode": bool(entry["track"].get("loop_mode")),
                    "is_priority": bool(entry["is_priority"]),
                    "queue_index": index,
                    "can_remove": bool(
                        viewer
                        and (
                            viewer.is_admin
                            or normalize_username(entry["track"].get("requested_by")) == viewer.normalized_username
                        )
                    ),
                }
                for index, entry in enumerate(preview_entries)
            ]
        if current and viewer:
            current["can_stop"] = bool(
                viewer.is_admin
                or normalize_username(current.get("requested_by")) == viewer.normalized_username
                or current.get("owner_id") == viewer.id
            )
            current["can_skip"] = self._viewer_can_skip_locked(viewer, preview_entries)
        return {
            "backend": self.backend_name,
            "current": current,
            "queue": queue,
            "last_error": self.last_error,
            "queue_limit": self.queue_limit,
            "volume": self.volume,
            "can_set_volume": bool(
                viewer and viewer.is_admin and self.backend and getattr(self.backend, "supports_web_volume", False)
            ),
        }

    def now_playing_state(self):
        with self.condition:
            current = dict(self.current) if self.current else None
        if not current:
            return {"status": "inactive"}

        elapsed_ms = self.backend.get_time_ms() if self.backend else None
        total_ms = self.backend.get_length_ms() if self.backend else None

        if elapsed_ms is None and current.get("started_at"):
            try:
                started_at = datetime.datetime.fromisoformat(current["started_at"].replace("Z", "+00:00"))
                elapsed_ms = max(0, int((datetime.datetime.now(datetime.timezone.utc) - started_at).total_seconds() * 1000))
            except Exception:
                elapsed_ms = None

        if total_ms is None and current.get("duration") is not None:
            try:
                total_ms = int(float(current["duration"]) * 1000)
            except Exception:
                total_ms = None

        remaining_ms = None
        if total_ms is not None and elapsed_ms is not None:
            remaining_ms = max(0, total_ms - elapsed_ms)

        return {
            "status": "playing",
            "title": current.get("nom"),
            "author": current.get("author"),
            "requested_by": current.get("requested_by"),
            "owner": current.get("owner"),
            "thumbnail_url": current.get("public_thumbnail_url") or current.get("cover_url"),
            "audio_path": current.get("rel_path"),
            "thumbnail_path": current.get("thumbnail_path"),
            "duration_seconds": round(total_ms / 1000, 2) if total_ms is not None else current.get("duration"),
            "elapsed_seconds": round(elapsed_ms / 1000, 2) if elapsed_ms is not None else None,
            "remaining_seconds": round(remaining_ms / 1000, 2) if remaining_ms is not None else None,
            "backend": self.backend_name,
        }


jukebox = JukeboxPlayer()


def build_track_payload_from_row(row, requested_by, loop_mode=False):
    absolute_path = absolute_user_path(row["owner_username"], row["file_path"])
    if not os.path.exists(absolute_path):
        raise FileNotFoundError(row["file_path"])
    cover_path = resolve_track_cover_path(row)
    cover_url = public_cover_url(row["owner_username"], cover_path)
    return {
        "track_id": row["id"],
        "owner_id": row["user_id"],
        "owner": row["owner_username"],
        "requested_by": requested_by,
        "rel_path": row["file_path"],
        "abs_path": absolute_path,
        "nom": row["title"] or row["logical_name"],
        "author": row["artist"] or row["owner_username"],
        "category": category_from_rel_path(row["owner_username"], row["file_path"]),
        "thumbnail_path": cover_path,
        "public_thumbnail_url": cover_url,
        "duration": row["duration"],
        "cover_url": cover_url,
        "loop_mode": bool(loop_mode),
    }


def build_track_payload_from_id(track_id, requested_by, loop_mode=False):
    row = get_track_row(track_id)
    if not row:
        raise FileNotFoundError(track_id)
    return build_track_payload_from_row(row, requested_by, loop_mode=loop_mode)


def build_track_payload_for_user(user, rel_path, loop_mode=False):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT t.*, u.username AS owner_username
            FROM tracks t
            JOIN users u ON u.id = t.user_id
            WHERE t.user_id = ? AND t.file_path = ?
            """,
            (user.id, rel_path),
        ).fetchone()
    if not row:
        raise FileNotFoundError(rel_path)
    return build_track_payload_from_row(row, user.username, loop_mode=loop_mode)


def update_sync_status(user_id, **data):
    with sync_status_lock:
        current = sync_status.get(user_id, {})
        current.update(data)
        sync_status[user_id] = current


def finish_sync_status(user_id):
    time.sleep(2)
    with sync_status_lock:
        sync_status.pop(user_id, None)


def is_retryable_youtube_error(message):
    if not message:
        return False
    lowered = message.lower()
    indicators = [
        "sign in to confirm",
        "confirm you're not a bot",
        "confirm youre not a bot",
        "age-restricted",
        "age restricted",
        "cookie",
        "http error 403",
        "forbidden",
        "signature",
        "nsig",
        "requested format is not available",
    ]
    return any(indicator in lowered for indicator in indicators)


def is_cookie_invalid_error(message):
    if not message:
        return False
    lowered = message.lower()
    return "cookies are no longer valid" in lowered or ("cookie" in lowered and "invalid" in lowered)


class YTDLPLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def debug(self, _message):
        return None

    def warning(self, message):
        self.warnings.append(str(message))
        print(f"[YT-DLP][WARN] {message}")

    def error(self, message):
        self.errors.append(str(message))
        print(f"[YT-DLP][ERROR] {message}")


def background_sync(username, user_id):
    print(f"[INFO] [{APP_NAME}] Synchronisation lancee pour {username}")
    user = User.get(user_id)
    if not user or not user.playlist_url:
        update_sync_status(user_id, progress=100, status="Termine (aucune playlist configuree)", filename="", last_error=None)
        finish_sync_status(user_id)
        return

    ensure_directory(user_audio_root(username))
    update_sync_status(user_id, progress=0, status="Demarrage...", filename="", last_error=None)

    cookie_file = user.cookies_path if user.cookies_path and os.path.exists(user.cookies_path) else None
    if user.cookies_path and not cookie_file:
        print(f"[WARN] Fichier cookies introuvable pour {username}: {user.cookies_path}")

    def hook(data):
        if data.get("status") == "downloading":
            raw_percent = data.get("_percent_str", "0%").replace("%", "").strip()
            try:
                progress = float(raw_percent)
            except Exception:
                progress = 0
            update_sync_status(
                user_id,
                progress=progress,
                status="Telechargement...",
                filename=os.path.basename(data.get("filename", "")),
            )
        elif data.get("status") == "finished":
            update_sync_status(user_id, progress=100, status="Conversion...", filename=os.path.basename(data.get("filename", "")))

    node_path = shutil.which("node") or shutil.which("nodejs")
    ffmpeg_ready = has_ffmpeg()
    destination = user_audio_root(username)
    base_ydl_opts = {
        "format": "bestaudio/best",
        "geo_bypass": True,
        "no_cache_dir": True,
        "outtmpl": os.path.join(destination, "%(playlist_title|Nixify)s", "%(title)s [%(id)s].%(ext)s"),
        "download_archive": os.path.join(destination, "archive.txt"),
        "max_filesize": MAX_TRACK_FILESIZE,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "writethumbnail": True,
        "writeinfojson": True,
        "updatetime": False,
    }
    if ffmpeg_ready:
        base_ydl_opts["postprocessors"] = [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegExtractAudio", "preferredcodec": "flac"},
            {"key": "FFmpegMetadata"},
        ]
    else:
        print("[WARN] FFmpeg absent: telechargement brut sans conversion FLAC ni miniatures.")
        update_sync_status(
            user_id,
            status="Telechargement sans FFmpeg",
            last_error="FFmpeg absent: conversion FLAC et miniatures desactivees.",
        )
    if cookie_file:
        base_ydl_opts["cookiefile"] = cookie_file
    if os.path.exists(FFMPEG_PATH):
        base_ydl_opts["ffmpeg_location"] = BASE_DIR
    if node_path:
        base_ydl_opts["javascript_executor"] = node_path

    attempts = [
        {"name": "standard", "player_clients": None, "use_cookies": True},
        {"name": "fallback-web-tv", "player_clients": ["web", "tv"], "use_cookies": True},
        {"name": "fallback-tv-no-cookies", "player_clients": ["tv"], "use_cookies": False},
    ]
    last_error = None

    for attempt in attempts:
        logger = YTDLPLogger()
        ydl_opts = dict(base_ydl_opts)
        ydl_opts["logger"] = logger
        if attempt["player_clients"]:
            ydl_opts["extractor_args"] = {"youtube": {"player_client": attempt["player_clients"]}}
        if not attempt["use_cookies"]:
            ydl_opts.pop("cookiefile", None)

        update_sync_status(user_id, status=f"Telechargement ({attempt['name']})...", progress=0)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.download([user.playlist_url])
            if result == 0:
                sync_user_library(user_id, username)
                update_sync_status(user_id, progress=100, status="Synchronisation terminee", filename="", last_error=None)
                finish_sync_status(user_id)
                return
            combined = " | ".join(logger.errors + logger.warnings).strip()
            last_error = f"yt-dlp code retour {result}. {combined}".strip()
        except Exception as exc:
            combined = " | ".join(logger.errors + logger.warnings).strip()
            last_error = f"{exc}. {combined}".strip()

        update_sync_status(user_id, last_error=(last_error or "")[:240], status=f"Echec ({attempt['name']})")
        if attempt["name"] == "standard" and is_retryable_youtube_error(last_error):
            continue
        if attempt["name"] == "fallback-web-tv" and is_cookie_invalid_error(last_error):
            continue
        break

    update_sync_status(user_id, progress=100, status="Synchronisation terminee avec erreur", filename="", last_error=(last_error or "")[:240])
    finish_sync_status(user_id)


def check_system():
    ensure_directory(os.path.join(BASE_DIR, "templates"))
    ensure_directory(COOKIES_DIR)
    ensure_directory(AUDIO_ROOT)


def require_admin():
    if not current_user.is_admin:
        abort(403)


def fetch_admin_users():
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.username, u.role, u.playlist_url, u.cookies_path,
                   COUNT(t.id) AS track_count
            FROM users u
            LEFT JOIN tracks t ON t.user_id = u.id
            GROUP BY u.id
            ORDER BY LOWER(u.username), u.id
            """
        ).fetchall()
    return rows


@app.errorhandler(RequestEntityTooLarge)
def handle_large_upload(_error):
    flash(f"L'upload depasse la limite autorisee par Nixify ({MAX_FOLDER_IMPORT_LABEL} max pour un import de dossier).", "error")
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return redirect(url_for("login"))


@app.route("/cover/<username>/<path:filename>")
@login_required
def get_cover(username, filename):
    if username != current_user.username and not current_user.is_admin:
        abort(403)
    try:
        absolute_path = absolute_user_path(username, filename)
    except PermissionError:
        abort(403)
    if not os.path.exists(absolute_path):
        return "", 404
    return send_file(absolute_path)


@app.route("/public-cover/<username>/<path:filename>")
def public_cover(username, filename):
    try:
        absolute_path = absolute_user_path(username, filename)
    except PermissionError:
        abort(403)
    if not os.path.exists(absolute_path):
        return "", 404
    return send_file(absolute_path)


@app.route("/public-cover-preview/<username>/<path:filename>")
def public_cover_preview(username, filename):
    try:
        absolute_path = absolute_user_path(username, filename)
    except PermissionError:
        abort(403)
    if not os.path.exists(absolute_path):
        return "", 404
    return send_file(build_web_cover_preview(absolute_path), conditional=True, max_age=604800)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = normalize_username(request.form["username"])
        password = request.form["password"]
        playlist_url = request.form.get("playlist_url", "").strip() or None
        invite_code = request.form.get("invite_code", "").strip()

        if not username:
            flash("Nom d'utilisateur invalide.", "error")
            return render_template("register.html", app_name=APP_NAME, footer_credit=FOOTER_CREDIT)

        if is_reserved_username(username):
            flash("Ce pseudo est réservé.", "error")
            return render_template("register.html", app_name=APP_NAME, footer_credit=FOOTER_CREDIT)

        if invite_code != DEFAULT_INVITE_CODE:
            flash("Code du club invalide.", "error")
            return render_template("register.html", app_name=APP_NAME, footer_credit=FOOTER_CREDIT)

        password_hash = generate_password_hash(password, method="scrypt")
        with get_db_connection() as conn:
            try:
                total = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
                role = "admin" if total == 0 else "user"
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, playlist_url) VALUES (?, ?, ?, ?)",
                    (username, password_hash, role, playlist_url),
                )
                conn.commit()
                ensure_directory(user_audio_root(username))
                flash("Compte Nixify cree.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("Ce pseudo est deja pris.", "error")
    return render_template("register.html", app_name=APP_NAME, footer_credit=FOOTER_CREDIT)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = normalize_username(request.form["username"])
        password = request.form["password"]
        user, password_hash = User.get_by_username(username)
        if not user:
            flash("Ce nom d'utilisateur n'existe pas.", "error")
        elif not check_password_hash(password_hash, password):
            flash("Mot de passe incorrect.", "error")
        else:
            login_user(user)
            return redirect(url_for("index"))
    return render_template("login.html", app_name=APP_NAME, footer_credit=FOOTER_CREDIT)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/admin")
@login_required
def admin_panel():
    require_admin()
    return render_template(
        "admin.html",
        app_name=APP_NAME,
        user=User.get(current_user.id),
        users=fetch_admin_users(),
        footer_credit=FOOTER_CREDIT,
    )


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
def admin_delete_user(user_id):
    require_admin()
    target = User.get(user_id)
    if not target:
        flash("Utilisateur introuvable.", "error")
        return redirect(url_for("admin_panel"))
    if target.id == current_user.id:
        flash("Vous ne pouvez pas supprimer votre propre compte depuis ce panneau.", "error")
        return redirect(url_for("admin_panel"))

    audio_root = user_audio_root(target.username)
    cookie_path = target.cookies_path
    jukebox.purge_matching(
        lambda item: (
            item.get("requested_by") == target.username
            or item.get("owner") == target.username
            or item.get("owner_id") == target.id
        )
    )

    with get_db_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (target.id,))
        conn.commit()

    if cookie_path and os.path.exists(cookie_path):
        try:
            os.remove(cookie_path)
        except OSError as exc:
            print(f"[WARN] Suppression cookie impossible pour {target.username}: {exc}")

    if os.path.exists(audio_root):
        shutil.rmtree(audio_root, ignore_errors=True)

    flash(f"Utilisateur supprime: {target.username}", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
def admin_reset_password(user_id):
    require_admin()
    target = User.get(user_id)
    if not target:
        flash("Utilisateur introuvable.", "error")
        return redirect(url_for("admin_panel"))

    new_password = (request.form.get("new_password") or "").strip()
    generated = False
    if not new_password:
        new_password = generate_temporary_password()
        generated = True

    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password, method="scrypt"), target.id),
        )
        conn.commit()

    if generated:
        flash(f"Mot de passe temporaire pour {target.username}: {new_password}", "success")
    else:
        flash(f"Mot de passe mis a jour pour {target.username}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/")
@login_required
def index():
    user = User.get(current_user.id)
    dashboard = build_dashboard_payload(user)
    return render_template(
        "index.html",
        app_name=APP_NAME,
        user=user,
        dashboard_json=json.dumps(dashboard, ensure_ascii=False),
        backend_name=jukebox.backend_name,
        server_ip=get_local_ip(),
        max_track_filesize_label=MAX_TRACK_FILESIZE_LABEL,
        max_image_upload_label="10 Mo",
        max_folder_import_label=MAX_FOLDER_IMPORT_LABEL,
        footer_credit=FOOTER_CREDIT,
    )


@app.route("/dashboard_data")
@login_required
def dashboard_data():
    return jsonify(build_dashboard_payload(User.get(current_user.id)))


@app.route("/player_state")
@login_required
def player_state():
    return jsonify(jukebox.snapshot(User.get(current_user.id)))


@app.route("/api/now-playing")
def api_now_playing():
    return jsonify(jukebox.now_playing_state())


@app.route("/now-playing")
def now_playing():
    return render_template(
        "now_playing.html",
        app_name=APP_NAME,
        server_ip=get_local_ip(),
        server_host=get_display_host(),
        server_port=SERVER_PORT,
        footer_credit=FOOTER_CREDIT,
    )


@app.route("/favorite/<int:track_id>", methods=["POST"])
@login_required
def toggle_favorite(track_id):
    if not get_track_row(track_id):
        return jsonify({"status": "error", "message": "Morceau introuvable"}), 404
    with get_db_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND track_id = ?",
            (current_user.id, track_id),
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM favorites WHERE user_id = ? AND track_id = ?", (current_user.id, track_id))
            message = "Retire des favoris."
        else:
            conn.execute("INSERT INTO favorites (user_id, track_id) VALUES (?, ?)", (current_user.id, track_id))
            message = "Ajoute aux favoris."
        conn.commit()
    return jsonify({"status": "ok", "message": message})


@app.route("/favorites/enqueue", methods=["POST"])
@login_required
def enqueue_favorites():
    payload = request.get_json(silent=True) or {}
    loop_mode = bool(payload.get("loop_mode"))
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.*, u.username AS owner_username
            FROM favorites f
            JOIN tracks t ON t.id = f.track_id
            JOIN users u ON u.id = t.user_id
            WHERE f.user_id = ?
            ORDER BY LOWER(COALESCE(t.title, t.logical_name)), t.id
            """,
            (current_user.id,),
        ).fetchall()
    if not rows:
        return jsonify({"status": "error", "message": "Aucun favori a envoyer."}), 400
    try:
        tracks = [build_track_payload_from_row(row, current_user.username, loop_mode=loop_mode) for row in rows]
    except (PermissionError, FileNotFoundError):
        return jsonify({"status": "error", "message": "Un favori est introuvable."}), 404
    ok, message = jukebox.enqueue_many(tracks)
    if not ok:
        return jsonify({"status": "error", "message": message}), 409
    return jsonify(
        {
            "status": "ok",
            "message": f"Favoris envoyes ({len(tracks)} morceau(x){' en boucle' if loop_mode else ''}).",
        }
    )


@app.route("/playlists", methods=["POST"])
@login_required
def create_playlist():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Nom de playlist manquant."}), 400

    with get_db_connection() as conn:
        duplicate = conn.execute(
            """
            SELECT 1 FROM playlists
            WHERE user_id = ? AND LOWER(name) = LOWER(?) AND (url IS NULL OR TRIM(url) = '')
            """,
            (current_user.id, name),
        ).fetchone()
        if duplicate:
            return jsonify({"status": "error", "message": "Une playlist porte deja ce nom."}), 409
        conn.execute("INSERT INTO playlists (user_id, name, url) VALUES (?, ?, NULL)", (current_user.id, name))
        conn.commit()
    return jsonify({"status": "ok", "message": "Playlist creee."})


def owned_playlist_or_404(playlist_id):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM playlists
            WHERE id = ? AND user_id = ? AND (url IS NULL OR TRIM(url) = '')
            """,
            (playlist_id, current_user.id),
        ).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/playlist/<int:playlist_id>/tracks/<int:track_id>", methods=["POST"])
@login_required
def add_track_to_playlist(playlist_id, track_id):
    owned_playlist_or_404(playlist_id)
    if not get_track_row(track_id):
        return jsonify({"status": "error", "message": "Morceau introuvable."}), 404

    with get_db_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            (playlist_id, track_id),
        ).fetchone()
        if exists:
            return jsonify({"status": "error", "message": "Ce morceau est deja dans la playlist."}), 409
        max_position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS max_position FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()["max_position"]
        conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            (playlist_id, track_id, max_position + 1),
        )
        conn.commit()
    return jsonify({"status": "ok", "message": "Morceau ajoute a la playlist."})


@app.route("/playlist/<int:playlist_id>/tracks/<int:track_id>/remove", methods=["POST"])
@login_required
def remove_track_from_playlist(playlist_id, track_id):
    owned_playlist_or_404(playlist_id)
    with get_db_connection() as conn:
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
        conn.commit()
    return jsonify({"status": "ok", "message": "Morceau retire de la playlist."})


@app.route("/playlist/<int:playlist_id>/delete", methods=["POST"])
@login_required
def delete_playlist(playlist_id):
    owned_playlist_or_404(playlist_id)
    with get_db_connection() as conn:
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        conn.commit()
    return jsonify({"status": "ok", "message": "Playlist supprimee."})


@app.route("/enqueue", methods=["POST"])
@login_required
def enqueue_track():
    payload = request.get_json(silent=True) or {}
    track_id = payload.get("track_id")
    rel_path = (payload.get("file_path") or "").strip()
    loop_mode = bool(payload.get("loop_mode"))
    if not track_id and not rel_path:
        return jsonify({"status": "error", "message": "Fichier manquant"}), 400
    try:
        if track_id:
            track = build_track_payload_from_id(int(track_id), current_user.username, loop_mode=loop_mode)
        else:
            track = build_track_payload_for_user(current_user, rel_path, loop_mode=loop_mode)
    except (PermissionError, FileNotFoundError):
        return jsonify({"status": "error", "message": "Morceau introuvable"}), 404
    ok, message = jukebox.enqueue(track)
    if not ok:
        return jsonify({"status": "error", "message": message}), 409
    return jsonify({"status": "ok", "message": "Ajoute a la file d'attente." if not loop_mode else "Ajoute a la file d'attente en boucle."})


@app.route("/play_next", methods=["POST"])
@login_required
def play_next():
    require_admin()
    payload = request.get_json(silent=True) or {}
    track_id = payload.get("track_id")
    rel_path = (payload.get("file_path") or "").strip()
    loop_mode = bool(payload.get("loop_mode"))
    if not track_id and not rel_path:
        return jsonify({"status": "error", "message": "Fichier manquant"}), 400
    try:
        if track_id:
            track = build_track_payload_from_id(int(track_id), current_user.username, loop_mode=loop_mode)
        else:
            track = build_track_payload_for_user(current_user, rel_path, loop_mode=loop_mode)
    except (PermissionError, FileNotFoundError):
        return jsonify({"status": "error", "message": "Morceau introuvable"}), 404
    ok, message = jukebox.enqueue(track, play_next=True)
    if not ok:
        return jsonify({"status": "error", "message": message}), 409
    return jsonify({"status": "ok", "message": "Le morceau passera juste apres la lecture en cours."})


@app.route("/play_now", methods=["POST"])
@login_required
def play_now():
    require_admin()
    payload = request.get_json(silent=True) or {}
    track_id = payload.get("track_id")
    rel_path = (payload.get("file_path") or "").strip()
    loop_mode = bool(payload.get("loop_mode"))
    if not track_id and not rel_path:
        return jsonify({"status": "error", "message": "Fichier manquant"}), 400
    try:
        if track_id:
            track = build_track_payload_from_id(int(track_id), current_user.username, loop_mode=loop_mode)
        else:
            track = build_track_payload_for_user(current_user, rel_path, loop_mode=loop_mode)
    except (PermissionError, FileNotFoundError):
        return jsonify({"status": "error", "message": "Morceau introuvable"}), 404
    ok, message = jukebox.enqueue(track, play_now=True)
    if not ok:
        return jsonify({"status": "error", "message": message}), 409
    return jsonify({"status": "ok", "message": "Lecture immediate demandee."})


@app.route("/stop_playback", methods=["POST"])
@login_required
def stop_playback():
    state = jukebox.snapshot(User.get(current_user.id))
    current = state.get("current")
    if not current:
        return jsonify({"status": "error", "message": "Aucune lecture en cours."}), 400
    if not current.get("can_stop") and not current_user.is_admin:
        return jsonify({"status": "error", "message": "Arret non autorise pour ce morceau."}), 403
    if not jukebox.skip():
        return jsonify({"status": "error", "message": "Aucune lecture en cours."}), 400
    return jsonify({"status": "ok", "message": "Lecture arretee."})


@app.route("/set_volume", methods=["POST"])
@login_required
def set_volume():
    require_admin()
    payload = request.get_json(silent=True) or {}
    if "volume" not in payload:
        return jsonify({"status": "error", "message": "Volume manquant."}), 400
    ok, message = jukebox.set_volume(payload.get("volume"))
    if not ok:
        return jsonify({"status": "error", "message": message}), 409
    return jsonify({"status": "ok", "message": f"Volume regle a {jukebox.volume}%.", "volume": jukebox.volume})


@app.route("/playlist/<int:playlist_id>/enqueue", methods=["POST"])
@login_required
def enqueue_playlist(playlist_id):
    owned_playlist_or_404(playlist_id)
    payload = request.get_json(silent=True) or {}
    loop_mode = bool(payload.get("loop_mode"))
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.*, u.username AS owner_username
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            JOIN users u ON u.id = t.user_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position, pt.rowid
            """,
            (playlist_id,),
        ).fetchall()
    if not rows:
        return jsonify({"status": "error", "message": "La playlist est vide."}), 400
    try:
        tracks = [build_track_payload_from_row(row, current_user.username, loop_mode=loop_mode) for row in rows]
    except (PermissionError, FileNotFoundError):
        return jsonify({"status": "error", "message": "Un morceau de la playlist est introuvable."}), 404
    ok, message = jukebox.enqueue_many(tracks)
    if not ok:
        return jsonify({"status": "error", "message": message}), 409
    return jsonify(
        {
            "status": "ok",
            "message": f"Playlist envoyee ({len(tracks)} morceau(x){' en boucle' if loop_mode else ''}).",
        }
    )


@app.route("/queue/clear_mine", methods=["POST"])
@app.route("/queue/clear-my-tracks", methods=["POST"])
@login_required
def clear_my_queue():
    removed = jukebox.clear_user_requests(current_user.username)
    if not removed:
        return jsonify({"status": "error", "message": "Aucun de vos morceaux n'etait dans la file."}), 400
    return jsonify({"status": "ok", "message": f"{removed} morceau(x) retire(s) de la file."})


@app.route("/queue/remove/<int:queue_index>", methods=["POST"])
@login_required
def remove_queue_item(queue_index):
    ok, message = jukebox.remove_queue_item(queue_index, User.get(current_user.id))
    if not ok:
        status = 403 if "interdit" in message.lower() else 404
        return jsonify({"status": "error", "message": message}), status
    return jsonify({"status": "ok", "message": message})


def matching_sidecar_json_path(rel_audio_path):
    parent = os.path.dirname(rel_audio_path)
    stem = os.path.splitext(os.path.basename(rel_audio_path))[0]
    filename = f"{stem}.info.json"
    return os.path.join(parent, filename).replace("\\", "/") if parent else filename


def rename_track_assets(row, requested_name):
    display_name = normalized_track_title(requested_name)
    if not display_name:
        raise ValueError("Nom invalide.")

    new_stem = sanitize_track_stem(display_name)
    if not new_stem:
        raise ValueError("Nom de fichier invalide.")

    old_rel_audio = row["file_path"]
    old_audio_stem = os.path.splitext(os.path.basename(old_rel_audio))[0]
    audio_ext = os.path.splitext(old_rel_audio)[1]
    new_audio_filename = f"{new_stem}{audio_ext}"
    new_rel_audio = os.path.join(os.path.dirname(old_rel_audio), new_audio_filename).replace("\\", "/")

    with get_db_connection() as conn:
        duplicate = conn.execute(
            """
            SELECT 1 FROM tracks
            WHERE user_id = ? AND id != ? AND LOWER(logical_name) = LOWER(?)
            """,
            (row["user_id"], row["id"], new_stem),
        ).fetchone()
    if duplicate:
        raise FileExistsError("Un autre morceau utilise deja ce nom.")

    resolved_cover_rel = resolve_track_cover_path(row)
    cover_rel_for_db = resolved_cover_rel

    cover_usage_count = 0
    if resolved_cover_rel:
        with get_db_connection() as conn:
            cover_usage_count = conn.execute(
                "SELECT COUNT(*) AS total FROM tracks WHERE user_id = ? AND id != ? AND cover_path = ?",
                (row["user_id"], row["id"], resolved_cover_rel),
            ).fetchone()["total"]

    operations = []
    username = row["owner_username"]
    old_audio_abs = absolute_user_path(username, old_rel_audio)
    new_audio_abs = absolute_user_path(username, new_rel_audio)
    if new_audio_abs != old_audio_abs:
        if os.path.exists(new_audio_abs):
            raise FileExistsError("Le fichier audio cible existe deja.")
        operations.append((old_audio_abs, new_audio_abs))

    old_info_rel = matching_sidecar_json_path(old_rel_audio)
    new_info_rel = matching_sidecar_json_path(new_rel_audio)
    old_info_abs = absolute_user_path(username, old_info_rel)
    new_info_abs = absolute_user_path(username, new_info_rel)
    if new_info_abs != old_info_abs and os.path.exists(old_info_abs):
        if os.path.exists(new_info_abs):
            raise FileExistsError("Le fichier metadata cible existe deja.")
        operations.append((old_info_abs, new_info_abs))

    if resolved_cover_rel and cover_usage_count == 0 and cover_should_follow_track_identity(row, resolved_cover_rel):
        cover_ext = os.path.splitext(resolved_cover_rel)[1]
        new_cover_filename = f"{new_stem}{cover_ext}"
        new_cover_rel = os.path.join(os.path.dirname(resolved_cover_rel), new_cover_filename).replace("\\", "/")
        old_cover_abs = absolute_user_path(username, resolved_cover_rel)
        new_cover_abs = absolute_user_path(username, new_cover_rel)
        if old_cover_abs != new_cover_abs and os.path.exists(old_cover_abs):
            if os.path.exists(new_cover_abs):
                raise FileExistsError("Le fichier miniature cible existe deja.")
            operations.append((old_cover_abs, new_cover_abs))
            cover_rel_for_db = new_cover_rel

    applied = []
    try:
        for source_path, target_path in operations:
            os.rename(source_path, target_path)
            applied.append((source_path, target_path))

        with get_db_connection() as conn:
            conn.execute(
                """
                UPDATE tracks
                SET logical_name = ?, title = ?, file_path = ?, cover_path = ?
                WHERE id = ?
                """,
                (new_stem, display_name, new_rel_audio, cover_rel_for_db, row["id"]),
            )
            conn.commit()
    except Exception:
        for source_path, target_path in reversed(applied):
            try:
                if os.path.exists(target_path):
                    os.rename(target_path, source_path)
            except OSError:
                pass
        raise

    return {"display_name": display_name, "logical_name": new_stem, "file_path": new_rel_audio, "cover_path": cover_rel_for_db}


@app.route("/delete/<int:track_id>", methods=["POST"])
@login_required
def delete_track(track_id):
    row = get_track_row(track_id)
    if not row:
        return jsonify({"status": "error", "message": "Morceau introuvable."}), 404
    if not can_delete_track(current_user, row):
        return jsonify({"status": "error", "message": "Suppression interdite."}), 403

    audio_path = absolute_track_audio_path(row)
    cover_path = absolute_track_cover_path(row)
    cover_usage_count = 0
    if row["cover_path"]:
        with get_db_connection() as conn:
            cover_usage_count = conn.execute(
                "SELECT COUNT(*) AS total FROM tracks WHERE user_id = ? AND id != ? AND cover_path = ?",
                (row["user_id"], track_id, row["cover_path"]),
            ).fetchone()["total"]

    jukebox.remove_track(track_id)

    with get_db_connection() as conn:
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        conn.commit()

    try:
        if os.path.exists(audio_path):
            os.remove(audio_path)
    except OSError as exc:
        print(f"[WARN] Suppression audio impossible pour {audio_path}: {exc}")

    if cover_path and cover_usage_count == 0 and cover_path != audio_path:
        try:
            if os.path.exists(cover_path):
                os.remove(cover_path)
        except OSError as exc:
            print(f"[WARN] Suppression miniature impossible pour {cover_path}: {exc}")

    mark_user_library_synced(row["user_id"])
    return jsonify({"status": "ok", "message": "Morceau supprime."})


@app.route("/rename/<int:track_id>", methods=["POST"])
@login_required
def rename_track(track_id):
    row = get_track_row(track_id)
    if not row:
        return jsonify({"status": "error", "message": "Morceau introuvable."}), 404
    if not can_delete_track(current_user, row):
        return jsonify({"status": "error", "message": "Renommage interdit."}), 403
    if jukebox.track_is_reserved(track_id):
        return jsonify({"status": "error", "message": "Retirez d'abord ce morceau de la file ou laissez finir la lecture."}), 409

    payload = request.get_json(silent=True) or {}
    requested_name = payload.get("new_name")
    if requested_name is None:
        return jsonify({"status": "error", "message": "Nouveau nom manquant."}), 400

    try:
        renamed = rename_track_assets(row, requested_name)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except FileExistsError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 409
    except (PermissionError, OSError) as exc:
        print(f"[WARN] Renommage impossible pour track {track_id}: {exc}")
        return jsonify({"status": "error", "message": "Renommage impossible pour ce morceau."}), 500

    mark_user_library_synced(row["user_id"])
    return jsonify({"status": "ok", "message": f"Morceau renomme en {renamed['display_name']}."})


@app.route("/skip", methods=["POST"])
@login_required
def skip_track():
    viewer = User.get(current_user.id)
    state = jukebox.snapshot(viewer)
    current = state.get("current")
    if not current:
        return jsonify({"status": "error", "message": "Aucune lecture en cours."}), 400
    if not current.get("can_skip"):
        return jsonify({"status": "error", "message": "Passage non autorise pour ce morceau."}), 403
    if not jukebox.skip():
        return jsonify({"status": "error", "message": "Aucune lecture en cours."}), 400
    return jsonify({"status": "ok", "message": "Passage au morceau suivant."})


@app.route("/sync")
@login_required
def sync():
    with sync_status_lock:
        if current_user.id in sync_status:
            return jsonify({"status": "already_running", "message": "Synchronisation deja en cours"})
        sync_status[current_user.id] = {"progress": 0, "status": "Preparation...", "filename": "", "last_error": None}

    thread = threading.Thread(target=background_sync, args=(current_user.username, current_user.id), daemon=True)
    thread.start()
    return jsonify({"status": "started", "message": "Synchronisation lancee."})


@app.route("/sync_progress")
@login_required
def sync_progress():
    with sync_status_lock:
        status = sync_status.get(current_user.id)
    if not status:
        return jsonify({"progress": -1, "status": "Inactif"})
    response = dict(status)
    if response.get("last_error"):
        response["error"] = response["last_error"]
    return jsonify(response)


@app.route("/profile", methods=["POST"])
@login_required
def update_profile():
    playlist_url = request.form.get("playlist_url", "").strip() or None
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET playlist_url = ? WHERE id = ?", (playlist_url, current_user.id))
        conn.commit()
    flash("Playlist YouTube mise a jour.", "success")
    return redirect(url_for("index"))


@app.route("/upload_track", methods=["POST"])
@login_required
def upload_track():
    ok, message, _result = store_uploaded_track(
        current_user,
        request.files.get("audio_file"),
        request.files.get("image_file"),
        origin="upload_track",
    )
    flash(message, "success" if ok else "error")
    return redirect(url_for("index"))


@app.route("/import_folder", methods=["POST"])
@login_required
def import_folder():
    uploaded_files = [item for item in request.files.getlist("folder_files") if item and item.filename]
    print(f"[IMPORT_FOLDER] {current_user.username}: {len(uploaded_files)} fichier(s) recus")
    if not uploaded_files:
        flash("Aucun dossier ou fichier exploitable selectionne.", "error")
        return redirect(url_for("index"))

    audio_entries = []
    image_entries = {}
    duplicate_audio_stems = set()
    seen_names = []

    for uploaded in uploaded_files:
        original_name = os.path.basename(uploaded.filename)
        seen_names.append(original_name)
        kind, _ext = classify_uploaded_extension(original_name)
        if kind == "audio":
            stem = sanitize_track_stem(original_name)
            if any(existing_stem == stem for existing_stem, _ in audio_entries):
                duplicate_audio_stems.add(stem)
            audio_entries.append((stem, uploaded))
        elif kind == "image":
            stem = sanitize_track_stem(original_name)
            image_entries.setdefault(stem, uploaded)

    print(
        f"[IMPORT_FOLDER] {current_user.username}: audio={len(audio_entries)} image={len(image_entries)} "
        f"doublons_audio={len(duplicate_audio_stems)}"
    )

    if not audio_entries:
        print(f"[IMPORT_FOLDER][SKIP] {current_user.username}: aucun audio supporte detecte. Echantillon={seen_names[:12]}")
        flash("Le dossier ne contient aucun fichier audio supporte.", "error")
        return redirect(url_for("index"))

    imported_count = 0
    skipped_messages = []
    batch_seen = set()
    for stem, audio_file in audio_entries:
        if stem in duplicate_audio_stems:
            skipped_messages.append(f"{stem}: doublon audio dans le dossier")
            print(f"[IMPORT_FOLDER][SKIP] {current_user.username}: doublon audio dans le dossier pour {stem}")
            continue
        if stem in batch_seen:
            continue
        batch_seen.add(stem)
        ok, message, _result = store_uploaded_track(
            current_user,
            audio_file,
            image_entries.get(stem),
            origin="import_folder",
        )
        if ok:
            imported_count += 1
        else:
            skipped_messages.append(f"{stem}: {message}")

    if imported_count:
        summary = f"Import dossier termine: {imported_count} morceau(x) ajoute(s)."
        if skipped_messages:
            summary += f" {len(skipped_messages)} ignore(s)."
        flash(summary, "success")
    else:
        flash("Aucun morceau du dossier n'a pu etre ajoute.", "error")

    if skipped_messages:
        print(f"[IMPORT_FOLDER][SUMMARY] {current_user.username}: imported={imported_count} skipped={len(skipped_messages)} reasons={skipped_messages}")
        flash("Imports ignores: " + " | ".join(skipped_messages[:8]), "error")
    else:
        print(f"[IMPORT_FOLDER][SUMMARY] {current_user.username}: imported={imported_count} skipped=0")
    return redirect(url_for("index"))


@app.route("/upload_cookies", methods=["POST"])
@login_required
def upload_cookies():
    if "cookies_file" not in request.files:
        flash("Aucun fichier cookies selectionne.", "error")
        return redirect(url_for("index"))

    uploaded_file = request.files["cookies_file"]
    if not uploaded_file or not uploaded_file.filename:
        flash("Aucun fichier cookies selectionne.", "error")
        return redirect(url_for("index"))

    extension = os.path.splitext(uploaded_file.filename)[1].lower()
    if extension not in ALLOWED_COOKIE_EXTENSIONS:
        flash("Le fichier doit etre un cookies.txt.", "error")
        return redirect(url_for("index"))

    file_size = uploaded_file_size(uploaded_file)
    if file_size > MAX_COOKIE_UPLOAD_BYTES:
        flash(f"Le fichier cookies.txt depasse la limite de {MAX_COOKIE_UPLOAD_LABEL}.", "error")
        return redirect(url_for("index"))

    safe_name = safe_username_fragment(current_user.username)
    final_name = secure_filename(f"{safe_name}_cookies.txt")
    destination = os.path.join(COOKIES_DIR, final_name)
    uploaded_file.save(destination)
    print(f"[INFO] Cookies uploades pour {current_user.username}: {destination} ({file_size} bytes)")

    with get_db_connection() as conn:
        conn.execute("UPDATE users SET cookies_path = ? WHERE id = ?", (destination, current_user.id))
        conn.commit()

    flash("Fichier cookies personnel enregistre.", "success")
    return redirect(url_for("index"))


@app.route("/vban")
@login_required
def vban():
    return render_template("vban.html", app_name=APP_NAME, server_ip=get_local_ip(), footer_credit=FOOTER_CREDIT)


if __name__ == "__main__":
    run_file = os.path.abspath(__file__)
    print(f"[BOOT] build={APP_BUILD} file={run_file} cwd={os.getcwd()}")
    if STRICT_ENTRYPOINT and os.path.normpath(run_file) != os.path.normpath(EXPECTED_ENTRYPOINT):
        raise RuntimeError(f"Entrypoint invalide: {run_file} (attendu: {EXPECTED_ENTRYPOINT})")

    check_system()
    init_db()

    print("[INFO] Verification systeme :")
    print(f"   - FFmpeg : {'[OK] Trouve' if has_ffmpeg() else '[KO] Manquant'}")
    node = shutil.which('node') or shutil.which('nodejs')
    print(f"   - NodeJS : {'[OK] Trouve' if node else '[KO] Manquant'}")
    print(f"   - Backend audio : {jukebox.backend_name}")
    if os.environ.get("PYTHONUTF8") != "1":
        print("[WARN] PYTHONUTF8=1 recommande pour Nixify.")
    if not has_ffmpeg():
        print("[WARN] Installez FFmpeg pour activer conversion FLAC, miniatures et metadonnees.")
    if jukebox.backend_name == "indisponible":
        print("[WARN] Installez python-vlc, mpv, mplayer ou ffplay pour la lecture locale sur la chaine hi-fi.")

    print(f"[INFO] {APP_NAME} - Ready")
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)
