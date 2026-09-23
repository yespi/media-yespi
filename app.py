#!/usr/bin/env python3
"""Self-hosted media portal — vídeos GoPro y fotos en carpetas locales.

Auth: Google OAuth, usuario/clave local, o abierto (configurable). Sirve MEDIA_ROOT/videos y /photos,
genera thumbnails con ffmpeg, permite multi-selección y borrado
(con una sola confirmación en el cliente por selección).
"""
from __future__ import annotations

import hashlib
import html
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("MEDIA_ROOT", "/mnt/new/gopro"))
VIDEOS = ROOT / "videos"
PHOTOS = ROOT / os.environ.get("PHOTOS_SUBDIR", "photos")
THUMBS = ROOT / "thumbs"
PHOTO_THUMBS = ROOT / "thumbs_photos"
PROXIES = ROOT / "proxies"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp"}
PHOTO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".bmp": "image/bmp",
}
LIB = ROOT / "library_videos.json"
DOWNLOAD_STATE = ROOT / "download_state.json"
USERS_FILE = Path(os.environ.get("USERS_FILE", "/config/users.json"))
PORT = int(os.environ.get("PORT", "8098"))
HOST = os.environ.get("HOST", "0.0.0.0")
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "") or secrets.token_hex(32)
CALLBACK_URL = os.environ.get(
    "GOOGLE_REDIRECT_URI", "http://localhost:8098/auth/google/callback"
)
COOKIE = "media_session"
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "").strip()
_COOKIE_SECURE_RAW = os.environ.get("COOKIE_SECURE", "auto").strip().lower()
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")
AUTH_PUBLIC_READ = os.environ.get("AUTH_PUBLIC_READ", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
AUTH_LOCAL = os.environ.get("AUTH_LOCAL", "").strip().lower() in ("1", "true", "yes", "on")
_PUBLIC_GET_PATHS = frozenset(
    {
        "/",
        "/index.html",
        "/api/logo",
        "/api/videos",
        "/api/photos",
        "/api/thumb",
        "/api/photo",
        "/api/stream",
    }
)
_GUEST_USER = {"email": "guest@local", "name": "Invitado", "admin": True}
PORTAL_BRAND = os.environ.get("PORTAL_BRAND", "").strip()
PORTAL_LOGO_URL = os.environ.get("PORTAL_LOGO_URL", "").strip()
_PORTAL_LOGO_FILE = os.environ.get("PORTAL_LOGO_FILE", "").strip()


def _portal_logo_path() -> Path | None:
    if _PORTAL_LOGO_FILE:
        p = Path(_PORTAL_LOGO_FILE)
        return p if p.is_file() else None
    default = USERS_FILE.parent / "logo.png"
    return default if default.is_file() else None


def _portal_logo_url() -> str:
    if PORTAL_LOGO_URL:
        return PORTAL_LOGO_URL
    if _portal_logo_path():
        return "/api/logo"
    return ""


def _portal_brand_title() -> str:
    return PORTAL_BRAND or "Media Portal"


def _portal_brand_html() -> str:
    title = _portal_brand_title()
    logo = _portal_logo_url()
    if logo:
        return (
            f'<img class="brand-logo" src="{html.escape(logo)}" '
            f'alt="{html.escape(title)}">'
        )
    return html.escape(title)


def _index_page() -> str:
    return INDEX_HTML.replace("{{PAGE_TITLE}}", html.escape(_portal_brand_title())).replace(
        "{{BRAND_HTML}}", _portal_brand_html()
    )


def _cookie_secure() -> bool:
    if _COOKIE_SECURE_RAW in ("0", "false", "no", "off"):
        return False
    if _COOKIE_SECURE_RAW in ("1", "true", "yes", "on"):
        return True
    return CALLBACK_URL.startswith("https://")


def _session_cookie(email: str) -> str:
    parts = [
        f"{COOKIE}={urllib.parse.quote(sign(email))}",
        "HttpOnly",
        "Path=/",
        "SameSite=Lax",
        "Max-Age=86400",
    ]
    if COOKIE_DOMAIN:
        parts.append(f"Domain={COOKIE_DOMAIN}")
    if _cookie_secure():
        parts.append("Secure")
    return "; ".join(parts)


def _clear_session_cookie() -> str:
    parts = [f"{COOKIE}=", "HttpOnly", "Path=/", "Max-Age=0"]
    if COOKIE_DOMAIN:
        parts.append(f"Domain={COOKIE_DOMAIN}")
    if _cookie_secure():
        parts.append("Secure")
    return "; ".join(parts)
PKCE_STORE: dict[str, dict] = {}
PKCE_LOCK = threading.Lock()
PKCE_TTL_SEC = 7200


def pkce_from_oauth_state(state: str) -> dict | None:
    """Recupera verifier del parámetro state (sobrevive reinicios del servicio)."""
    if not state:
        return None
    try:
        data = json.loads(_unb64url(state))
        if time.time() - float(data.get("t", 0)) > PKCE_TTL_SEC:
            return None
        verifier = data.get("v")
        if verifier:
            return {"verifier": verifier}
    except Exception:
        return None
    return None
THUMB_SEM = threading.Semaphore(3)  # max concurrent ffmpeg thumbs
THUMB_PENDING: set[str] = set()
THUMB_LOCK = threading.Lock()
_PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="300" viewBox="0 0 480 300">'
    b'<rect width="480" height="300" fill="#0a0e0b"/>'
    b'<text x="240" y="150" fill="#8fa396" font-family="sans-serif" font-size="18" '
    b'text-anchor="middle">generando...</text></svg>'
)

THUMBS.mkdir(parents=True, exist_ok=True)
PHOTO_THUMBS.mkdir(parents=True, exist_ok=True)
PROXIES.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)
PHOTOS.mkdir(parents=True, exist_ok=True)


def _b64url(b: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64url(s: str) -> bytes:
    import base64

    s = s.replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s)


def sign(email: str) -> str:
    d = _b64url(email.encode())
    sig = hmac.new(SESSION_SECRET.encode(), d.encode(), hashlib.sha256).hexdigest()
    return f"{d}.{sig}"


def verify(tok: str | None) -> str | None:
    if not tok:
        return None
    parts = tok.split(".")
    if len(parts) != 2:
        return None
    d, s = parts
    expect = hmac.new(SESSION_SECRET.encode(), d.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, s):
        return None
    try:
        return _unb64url(d).decode()
    except Exception:
        return None


def load_users() -> list[dict]:
    if not USERS_FILE.is_file():
        return []
    try:
        return json.loads(USERS_FILE.read_text()).get("users") or []
    except Exception:
        return []


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, salt, hx = stored.split("$", 2)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
            return hmac.compare_digest(digest.hex(), hx)
        except Exception:
            return False
    return hmac.compare_digest(password, stored)


def user_for(ident: str | None) -> dict | None:
    if not ident:
        return None
    e = ident.lower()
    for u in load_users():
        if u.get("email", "").lower() == e or u.get("username", "").lower() == e:
            return u
    return None


def authenticate_local(username: str, password: str) -> dict | None:
    u = user_for(username)
    if not u:
        return None
    stored = u.get("password_hash") or u.get("password") or ""
    if not verify_password(password, stored):
        return None
    return u


def session_ident(user: dict) -> str:
    return (user.get("username") or user.get("email") or "").lower()


def parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = urllib.parse.unquote(v.strip())
    return out


def load_library() -> dict[str, dict]:
    """Map media-id prefix / filename → metadata."""
    by: dict[str, dict] = {}
    if not LIB.is_file():
        return by
    try:
        items = json.loads(LIB.read_text()).get("items") or []
    except Exception:
        return by
    for it in items:
        mid = str(it.get("id") or "")
        fn = str(it.get("filename") or "")
        if mid:
            by[mid] = it
            by[mid[:8]] = it
        if fn:
            by[fn] = it
            by[Path(fn).stem] = it
    return by


def _safe_rel_path(base: Path, rel: str) -> Path | None:
    rel = (rel or "").replace("\\", "/").strip("/")
    if rel and (".." in rel.split("/") or rel.startswith(".")):
        return None
    target = (base / rel).resolve() if rel else base.resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


def _sort_entries(entries: list[dict], sort: str, name_key: str = "name", date_key: str = "mtime") -> None:
    sk = (sort or "date_desc").lower()
    if sk == "name_asc":
        entries.sort(key=lambda x: str(x.get(name_key, "")).lower())
    elif sk == "name_desc":
        entries.sort(key=lambda x: str(x.get(name_key, "")).lower(), reverse=True)
    elif sk == "date_asc":
        entries.sort(key=lambda x: float(x.get(date_key) or 0))
    else:
        entries.sort(key=lambda x: float(x.get(date_key) or 0), reverse=True)


def _video_item(p: Path, base: Path, lib: dict) -> dict:
    rel = str(p.relative_to(base)).replace("\\", "/")
    stem = p.stem
    mid_pref = ""
    m = re.search(r"_([0-9a-f]{8})$", stem, re.I)
    if m:
        mid_pref = m.group(1).lower()
    meta = lib.get(mid_pref) or lib.get(p.name) or lib.get(stem) or {}
    if not meta and mid_pref:
        meta = lib.get(stem[: -(len(mid_pref) + 1)]) or {}
    captured = meta.get("captured_at") or ""
    st = p.stat()
    return {
        "id": mid_pref or stem,
        "media_id": meta.get("id") or mid_pref or stem,
        "path": rel,
        "filename": p.name,
        "title": meta.get("filename") or p.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "captured_at": captured,
        "camera": meta.get("camera_model") or "",
        "resolution": meta.get("resolution") or "",
        "duration_ms": meta.get("source_duration"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "codec": probe_codec(p, probe=False),
        "media_id_full": meta.get("id") or "",
    }


def _photo_item(p: Path, base: Path) -> dict:
    rel = str(p.relative_to(base)).replace("\\", "/")
    st = p.stat()
    return {
        "path": rel,
        "filename": p.name,
        "title": p.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def browse_media(kind: str, folder: str = "", sort: str = "date_desc", search: str = "") -> dict:
    base = VIDEOS if kind == "video" else PHOTOS
    exts = VIDEO_EXTS if kind == "video" else PHOTO_EXTS
    dir_path = _safe_rel_path(base, folder)
    if not dir_path or not dir_path.is_dir():
        return {"kind": kind, "folder": folder or "", "folders": [], "items": []}
    q = (search or "").strip().lower()
    lib = load_library() if kind == "video" else {}
    folders: list[dict] = []
    items: list[dict] = []
    for p in dir_path.iterdir():
        if p.name.startswith("."):
            continue
        if p.is_dir():
            rel = str(p.relative_to(base)).replace("\\", "/")
            st = p.stat()
            entry = {"name": p.name, "path": rel, "mtime": st.st_mtime}
            if q and q not in rel.lower() and q not in p.name.lower():
                continue
            folders.append(entry)
        elif p.is_file() and p.suffix.lower() in exts and not p.name.endswith(".part"):
            item = _video_item(p, base, lib) if kind == "video" else _photo_item(p, base)
            blob = " ".join(
                str(item.get(k) or "")
                for k in ("path", "filename", "title", "camera", "captured_at", "resolution")
            ).lower()
            if q and q not in blob:
                continue
            items.append(item)
    if kind == "video" and not q:
        items.sort(
            key=lambda v: v.get("captured_at") or str(v.get("mtime") or ""),
            reverse=(sort or "date_desc") != "date_asc",
        )
        if sort in ("name_asc", "name_desc"):
            items.sort(key=lambda v: v.get("filename", "").lower(), reverse=sort == "name_desc")
    else:
        _sort_entries(items, sort, name_key="filename", date_key="mtime")
    _sort_entries(folders, sort, name_key="name", date_key="mtime")
    return {"kind": kind, "folder": folder or "", "folders": folders, "items": items}


def list_videos() -> list[dict]:
    return browse_media("video", "", "date_desc")["items"]


def _thumb_key(rel: str) -> str:
    return rel.replace("/", "__")


def _thumb_path(video: Path, rel: str | None = None) -> Path:
    key = _thumb_key(rel) if rel else video.stem
    return THUMBS / (key + ".jpg")


def _photo_thumb_path(rel: str) -> Path:
    return PHOTO_THUMBS / (_thumb_key(rel) + ".jpg")


def _make_thumb(video: Path, rel: str | None = None) -> Path | None:
    """Synchronously generate one thumbnail (caller holds THUMB_SEM)."""
    thumb = _thumb_path(video, rel)
    if thumb.is_file() and thumb.stat().st_mtime >= video.stat().st_mtime:
        return thumb
    # Must end in .jpg — ffmpeg rejects ".jpg.part" (exit 234)
    key = _thumb_key(rel) if rel else video.stem
    tmp = THUMBS / (key + ".tmp.jpg")

    def _run(ss: str) -> bool:
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", ss, "-i", str(video),
                    "-frames:v", "1", "-an",
                    "-vf", "scale=480:-2",
                    "-q:v", "5",
                    "-update", "1",
                    str(tmp),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
            return tmp.is_file() and tmp.stat().st_size > 0
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"[media] thumb fail {video.name} ss={ss}: {e}", flush=True)
            return False

    if _run("1") or _run("0"):
        tmp.replace(thumb)
        return thumb
    tmp.unlink(missing_ok=True)
    return thumb if thumb.is_file() else None


def ensure_photo_thumb(photo: Path, rel: str, wait: bool = False) -> Path | None:
    thumb = _photo_thumb_path(rel)
    try:
        if thumb.is_file() and thumb.stat().st_mtime >= photo.stat().st_mtime:
            return thumb
    except Exception:
        pass
    if photo.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return photo
    tmp = thumb.with_suffix(".tmp.jpg")

    def _job():
        try:
            with THUMB_SEM:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(photo),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=480:-2",
                        "-q:v",
                        "5",
                        str(tmp),
                    ],
                    check=True,
                    timeout=90,
                )
                if tmp.is_file() and tmp.stat().st_size > 0:
                    tmp.replace(thumb)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"[media] photo thumb fail {rel}: {e}", flush=True)

    if wait:
        _job()
        return thumb if thumb.is_file() else None
    threading.Thread(target=_job, daemon=True).start()
    return thumb if thumb.is_file() else None


def ensure_thumb(video: Path, wait: bool = False, rel: str | None = None) -> Path | None:
    """Return thumb path if ready. If missing, queue generation (or wait if wait=True)."""
    thumb = _thumb_path(video, rel)
    try:
        if thumb.is_file() and thumb.stat().st_mtime >= video.stat().st_mtime:
            return thumb
    except Exception:
        pass
    key = str(video)
    with THUMB_LOCK:
        already = key in THUMB_PENDING
        if not already:
            THUMB_PENDING.add(key)

    def _job():
        try:
            with THUMB_SEM:
                _make_thumb(video, rel)
        finally:
            with THUMB_LOCK:
                THUMB_PENDING.discard(key)

    if wait:
        if not already:
            _job()
        else:
            # someone else is building it — brief wait
            for _ in range(40):
                if thumb.is_file():
                    return thumb
                time.sleep(0.25)
        return thumb if thumb.is_file() else None

    if not already:
        threading.Thread(target=_job, daemon=True).start()
    return thumb if thumb.is_file() else None


def probe_codec(video: Path, probe: bool = True) -> str:
    """Return video codec name (h264/hevc/…) cached beside thumbs."""
    cache = THUMBS / (video.stem + ".codec")
    try:
        if cache.is_file() and cache.stat().st_mtime >= video.stat().st_mtime:
            return cache.read_text().strip() or "unknown"
    except Exception:
        pass
    if not probe:
        # GoPro cloud files are almost always HEVC; avoid ffprobe storm on listing
        name = video.name.upper()
        if name.startswith("GX") or name.startswith("GH"):
            return "hevc"
        return "unknown"
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                str(video),
            ],
            text=True,
            timeout=30,
        ).strip().splitlines()
        codec = (out[0] if out else "unknown").strip() or "unknown"
    except Exception:
        codec = "unknown"
    try:
        cache.write_text(codec)
    except Exception:
        pass
    return codec


def warm_thumbs_background() -> None:
    """Pre-generate missing thumbs with limited concurrency."""
    def _run():
        time.sleep(2)
        vids = [
            p
            for p in VIDEOS.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.endswith(".part")
        ]
        vids.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        print(f"[media] warm_thumbs: {len(vids)} videos", flush=True)
        for v in vids:
            th = _thumb_path(v)
            if th.is_file() and th.stat().st_mtime >= v.stat().st_mtime:
                continue
            rel = str(v.relative_to(VIDEOS)).replace("\\", "/")
            ensure_thumb(v, wait=False, rel=rel)
            time.sleep(0.05)  # stagger queue
        print("[media] warm_thumbs: queue filled", flush=True)
    threading.Thread(target=_run, daemon=True).start()


def proxy_path(video: Path, rel: str | None = None) -> Path:
    key = _thumb_key(rel) if rel else video.stem
    return PROXIES / (key + ".play.mp4")


def mark_download_deleted(media_id: str | None, filename: str) -> None:
    """Remember intentional deletes so the GoPro downloader won't fetch them again."""
    if not media_id and not filename:
        return
    try:
        st = {"done": {}, "failed": {}, "deleted": {}}
        if DOWNLOAD_STATE.is_file():
            st.update(json.loads(DOWNLOAD_STATE.read_text()))
        st.setdefault("deleted", {})
        st.setdefault("done", {})
        key = media_id or filename
        st["deleted"][key] = {"at": time.time(), "filename": filename, "reason": "portal_delete"}
        if media_id:
            st["done"].pop(media_id, None)
        # also drop done entries pointing at this file
        for mid, info in list(st["done"].items()):
            if (info or {}).get("filename") == filename or str((info or {}).get("path") or "").endswith(filename):
                st["done"].pop(mid, None)
        tmp = DOWNLOAD_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2) + "\n")
        tmp.replace(DOWNLOAD_STATE)
    except Exception as e:
        print(f"[media] mark_download_deleted failed: {e}", flush=True)


def http_json(url: str, data: dict | None = None, headers: dict | None = None) -> dict:
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode(errors="replace")
        except Exception:
            pass
        print(f"[media] http_json {e.code} {url}: {err_body[:400]}", flush=True)
        raise


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%231a221c'/%3E%3Ctext x='16' y='22' text-anchor='middle' font-size='16' fill='%236bcb8a'%3E%E2%96%AC%3C/text%3E%3C/svg%3E">
<title>{{PAGE_TITLE}}</title>
<style>
:root{
  --bg:#0f1410; --surface:#1a221c; --surface2:#243028; --border:#2f3d34;
  --text:#e8efe9; --muted:#8fa396; --accent:#6bcb8a; --danger:#e86a6a; --warn:#e0b35a;
}
*{box-sizing:border-box}
body{margin:0;font-family:"IBM Plex Sans",system-ui,sans-serif;background:
  radial-gradient(1200px 600px at 10% -10%,#1c3324 0%,transparent 55%),
  radial-gradient(900px 500px at 100% 0%,#1a2830 0%,transparent 50%),
  var(--bg);color:var(--text);min-height:100vh}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
  padding:16px 20px;border-bottom:1px solid var(--border);backdrop-filter:blur(8px);
  position:sticky;top:0;z-index:20;background:rgba(15,20,16,.88)}
.brand{font-family:"Fraunces",Georgia,serif;font-size:1.45rem;letter-spacing:-.02em;display:flex;align-items:center}
.brand span{color:var(--accent)}
.brand-logo{max-height:42px;max-width:min(240px,50vw);object-fit:contain;display:block}
.muted{color:var(--muted);font-size:.9rem}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=search]{background:var(--surface);border:1px solid var(--border);color:var(--text);
  padding:8px 12px;border-radius:8px;min-width:200px}
button,.btn{background:var(--accent);color:#0c150f;border:0;padding:8px 14px;border-radius:8px;
  font-weight:650;cursor:pointer}
button.secondary{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
button.danger{background:var(--danger);color:#1a0a0a}
button:disabled{opacity:.45;cursor:not-allowed}
main{padding:16px 20px 80px;max-width:1400px;margin:0 auto}
.stats{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;
  position:relative;transition:border-color .15s,transform .15s}
.card:hover{border-color:var(--accent);transform:translateY(-1px)}
.card.selected{outline:2px solid var(--accent);border-color:var(--accent)}
.thumb{aspect-ratio:16/10;background:#0a0e0b;display:block;width:100%;object-fit:cover;cursor:pointer}
.meta{padding:10px 12px 12px}
.meta .title{font-size:.86rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:0 0 4px}
.meta .sub{font-size:.75rem;color:var(--muted)}
.check-label{position:absolute;top:8px;left:8px;z-index:6;width:30px;height:30px;
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  background:rgba(15,20,16,.92);border:1px solid #6bcb8a;border-radius:8px;
  box-shadow:0 2px 8px rgba(0,0,0,.45)}
.check-label:hover{background:rgba(26,34,28,.98);border-color:#9BE0B0}
.check{width:16px;height:16px;margin:0;accent-color:var(--accent);cursor:pointer}
.bar{position:fixed;bottom:0;left:0;right:0;padding:12px 20px;background:rgba(15,20,16,.95);
  border-top:1px solid var(--border);display:none;gap:12px;align-items:center;justify-content:space-between;z-index:30}
.bar.show{display:flex}
dialog{border:1px solid var(--border);border-radius:14px;padding:0;background:var(--surface);color:var(--text);
  max-width:420px;width:calc(100% - 32px)}
dialog::backdrop{background:rgba(0,0,0,.55)}
.dlg{padding:20px}
.dlg h2{margin:0 0 8px;font-size:1.15rem}
.dlg p{margin:0 0 16px;color:var(--muted);line-height:1.45}
.dlg .actions{display:flex;gap:8px;justify-content:flex-end}
.thumb-wrap{position:relative}
.thumb-wrap .play-badge{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.25);opacity:0;transition:opacity .15s;pointer-events:none;font-size:2rem}
.card:hover .play-badge{opacity:1}
#playerModal{position:fixed;inset:0;z-index:80;display:none;align-items:center;justify-content:center;
  padding:16px;background:rgba(0,0,0,.72);backdrop-filter:blur(6px)}
#playerModal.open{display:flex}
#playerShell{width:min(1100px,100%);background:#0a0e0b;border:1px solid var(--border);border-radius:14px;
  overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.45);display:flex;flex-direction:column;max-height:calc(100vh - 32px)}
#playerShell:fullscreen{width:100%;height:100%;max-height:none;border-radius:0;border:0}
#playerTop{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px 12px;
  background:rgba(15,20,16,.95);border-bottom:1px solid var(--border)}
#playerTop .tools{gap:6px}
#playerShell video{width:100%;max-height:min(75vh,820px);background:#000;vertical-align:middle}
#playerShell:fullscreen video{max-height:calc(100vh - 54px);height:calc(100vh - 54px)}
#playerStatus{padding:0 12px 10px;font-size:.85rem;color:var(--muted);min-height:1.2em}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tab{background:var(--surface2);color:var(--text);border:1px solid var(--border);padding:8px 16px;border-radius:8px;cursor:pointer;font-weight:600}
.tab.active{background:var(--accent);color:#0c150f;border-color:var(--accent)}
.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:10px}
.folder-row{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.folder-chip{background:var(--surface);border:1px solid var(--border);padding:6px 12px;border-radius:999px;cursor:pointer;font-size:.85rem}
.folder-chip:hover{border-color:var(--accent)}
#photoModal{position:fixed;inset:0;z-index:80;display:none;align-items:center;justify-content:center;padding:16px;background:rgba(0,0,0,.85)}
#photoModal.open{display:flex}
#photoModal img{max-width:min(1200px,100%);max-height:90vh;border-radius:8px}
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
</head>
<body>
<header>
  <div>
    <div class="brand">{{BRAND_HTML}}</div>
    <div class="muted" id="who">…</div>
  </div>
  <div class="tools">
    <input type="search" id="q" placeholder="Buscar en carpeta (nombre, ruta…)">
    <button class="secondary admin-only" id="selAll" type="button">Seleccionar visibles</button>
    <button class="secondary admin-only" id="selNone" type="button">Nada</button>
    <a class="btn secondary admin-only" id="loginLink" href="/auth/google/start" style="text-decoration:none;display:none">Entrar (admin)</a>
    <a class="btn secondary" id="logoutLink" href="/__logout" style="text-decoration:none;display:none">Salir</a>
  </div>
</header>
<main>
  <nav class="tabs">
    <button type="button" class="tab active" id="tabVideos">Vídeos</button>
    <button type="button" class="tab" id="tabPhotos">Fotos</button>
  </nav>
  <div class="toolbar">
    <div class="muted" id="breadcrumb">—</div>
    <select id="sortBy" aria-label="Ordenar">
      <option value="date_desc">Fecha (reciente)</option>
      <option value="date_asc">Fecha (antigua)</option>
      <option value="name_asc">Nombre A→Z</option>
      <option value="name_desc">Nombre Z→A</option>
    </select>
  </div>
  <div class="folder-row" id="folderRow"></div>
  <div class="stats">
    <div class="stat" id="statCount">—</div>
    <div class="stat" id="statSize">—</div>
  </div>
  <div class="grid" id="grid"></div>
</main>
<div id="photoModal" aria-hidden="true"><img id="photoFull" alt=""></div>
<div id="playerModal" aria-hidden="true">
  <div id="playerShell">
    <div id="playerTop">
      <div class="muted" id="playerTitle">—</div>
      <div class="tools">
        <button class="secondary" type="button" id="btnFs" title="Pantalla completa">Pantalla completa</button>
        <button class="secondary" type="button" id="closePlayer">Cerrar</button>
      </div>
    </div>
    <video id="player" controls playsinline preload="metadata"></video>
    <div id="playerStatus"></div>
  </div>
</div>
<div class="bar admin-only" id="bar">
  <div><b id="selCount">0</b> seleccionados</div>
  <div class="tools">
    <button class="danger" type="button" id="btnDelete">Eliminar selección</button>
  </div>
</div>
<dialog id="dlg">
  <div class="dlg">
    <h2>Confirmar borrado</h2>
    <p id="dlgText">¿Eliminar los vídeos seleccionados?</p>
    <div class="actions">
      <button class="secondary" type="button" id="dlgCancel">Cancelar</button>
      <button class="danger" type="button" id="dlgOk">Eliminar</button>
    </div>
  </div>
</dialog>
<dialog id="loginDlg">
  <div class="dlg">
    <h2>Iniciar sesión</h2>
    <p class="muted" style="margin-bottom:12px">Solo administradores (borrar vídeos/fotos).</p>
    <form id="loginForm">
      <label style="display:block;margin-bottom:8px">Usuario<br><input name="user" id="loginUser" autocomplete="username" required style="width:100%;margin-top:4px;padding:8px"></label>
      <label style="display:block;margin-bottom:16px">Contraseña<br><input name="pass" id="loginPass" type="password" autocomplete="current-password" required style="width:100%;margin-top:4px;padding:8px"></label>
      <div class="actions">
        <button type="button" class="secondary" id="loginCancel">Cancelar</button>
        <button type="submit" class="btn">Entrar</button>
      </div>
    </form>
    <p id="loginErr" class="muted" style="color:var(--danger);min-height:1.2em;margin-top:8px"></p>
  </div>
</dialog>
<script>
const state={tab:'video',folder:'',folders:[],items:[],selected:new Set(),filter:'',sort:'date_desc'};
const itemKey=v=>v.path||v.filename;
let searchTimer=null;
const session={email:null,username:null,name:null,admin:false,auth_disabled:false,public_read:false,auth_local:false};
const $=id=>document.getElementById(id);
const fmtBytes=n=>{if(n<1024)return n+' B';if(n<1e6)return(n/1024).toFixed(1)+' KB';if(n<1e9)return(n/1e6).toFixed(1)+' MB';return(n/1e9).toFixed(2)+' GB'};
const fmtDate=s=>{if(!s)return 'sin fecha';try{const d=new Date(s);return d.toLocaleString('es-ES',{dateStyle:'medium',timeStyle:'short'})}catch{return s}};

function applyUiMode(){
  const admin=session.admin||session.auth_disabled;
  document.querySelectorAll('.admin-only').forEach(el=>{el.style.display=admin?'':'none';});
  const logged=!!(session.email||session.username);
  const showLogin=!logged&&session.public_read&&!session.auth_disabled;
  $('loginLink').style.display=showLogin?'inline-flex':'none';
  if(session.auth_local&&showLogin){$('loginLink').setAttribute('href','#');}
  $('logoutLink').style.display=(logged&&!session.auth_disabled)?'inline-flex':'none';
}
async function who(){
  const r=await fetch('/__whoami');const u=await r.json();
  Object.assign(session,u);
  if(session.auth_disabled){$('who').textContent='acceso abierto (sin login)';}
  else if(session.email||session.username){$('who').textContent=`Hola, ${session.name||session.username||session.email}`+(session.admin?' · admin':'');}
  else if(session.public_read){$('who').textContent='visitante (ver vídeos y fotos, sin borrar)';}
  else{$('who').textContent='no autenticado';}
  applyUiMode();
}

async function load(){
  const base=state.tab==='photo'?'/api/photos':'/api/videos';
  const u=`${base}?folder=${encodeURIComponent(state.folder)}&sort=${encodeURIComponent(state.sort)}&q=${encodeURIComponent(state.filter)}`;
  const r=await fetch(u); const j=await r.json();
  state.folders=j.folders||[]; state.items=j.items||[];
  render();
}

function renderBreadcrumb(){
  const parts=state.folder?state.folder.split('/'):[];
  let html=`<a href="#" data-crumb="">Raíz</a>`;
  let acc='';
  for(const p of parts){
    acc=acc?acc+'/'+p:p;
    html+=` / <a href="#" data-crumb="${acc}">${p}</a>`;
  }
  $('breadcrumb').innerHTML=html;
  $('breadcrumb').querySelectorAll('a').forEach(a=>a.onclick=e=>{e.preventDefault();state.folder=a.dataset.crumb||'';state.selected.clear();load();});
}

function renderFolders(){
  const row=$('folderRow'); row.innerHTML='';
  for(const f of state.folders){
    const b=document.createElement('button');
    b.type='button'; b.className='folder-chip'; b.textContent='📁 '+f.name;
    b.onclick=()=>{state.folder=f.path;state.selected.clear();load();};
    row.appendChild(b);
  }
}

function render(){
  renderBreadcrumb();
  renderFolders();
  const list=state.items;
  const label=state.tab==='photo'?'fotos':'vídeos';
  const totalBytes=list.reduce((a,v)=>a+(v.size||0),0);
  $('statCount').textContent=`${list.length} ${label}`+(state.folders.length?` · ${state.folders.length} carpetas`:'');
  $('statSize').textContent=fmtBytes(totalBytes);
  const g=$('grid'); g.innerHTML='';
  const isPhoto=state.tab==='photo';
  const thumbKind=isPhoto?'photo':'video';
  for(const v of list){
    const key=itemKey(v);
    const card=document.createElement('article');
    card.className='card'+(state.selected.has(key)?' selected':'');
    const admin=session.admin||session.auth_disabled;
    const codecLabel=!isPhoto&&v.codec&&v.codec!=='unknown'?' · '+v.codec:'';
    const subDate=isPhoto?fmtDate(v.mtime?new Date(v.mtime*1000).toISOString():''):fmtDate(v.captured_at);
    card.innerHTML=`
      <div class="thumb-wrap">
        <img class="thumb" data-file="${key}" src="/api/thumb?kind=${thumbKind}&file=${encodeURIComponent(key)}&t=1" alt="" loading="lazy">
        <div class="play-badge">${isPhoto?'🖼':'▶'}</div>
        <label class="check-label" title="Seleccionar para borrar">
          <input class="check" type="checkbox" ${state.selected.has(key)?'checked':''} aria-label="Seleccionar">
        </label>
      </div>
      <div class="meta">
        <p class="title" title="${v.title}">${v.title}</p>
        <div class="sub">${subDate} · ${fmtBytes(v.size||0)}${v.camera?' · '+v.camera:''}${codecLabel}</div>
      </div>`;
    const cb=card.querySelector('.check');
    const lab=card.querySelector('.check-label');
    lab.addEventListener('click',e=>e.stopPropagation());
    cb.addEventListener('click',e=>e.stopPropagation());
    cb.addEventListener('change',()=>{
      if(cb.checked) state.selected.add(key); else state.selected.delete(key);
      card.classList.toggle('selected', cb.checked); syncBar();
    });
    if(!admin) lab.style.display='none';
    card.querySelector('.thumb-wrap').addEventListener('click',()=>isPhoto?viewPhoto(v):play(v));
    g.appendChild(card);
  }
  syncBar();
  if(!isPhoto) wireThumbs(g);
}

function viewPhoto(v){
  const key=itemKey(v);
  $('photoFull').src='/api/photo?file='+encodeURIComponent(key);
  $('photoModal').classList.add('open');
}
$('photoModal').onclick=()=>$('photoModal').classList.remove('open');

function syncBar(){
  const n=state.selected.size;
  $('selCount').textContent=String(n);
  $('bar').classList.toggle('show', n>0);
}
function wireThumbs(root){
  root.querySelectorAll('img.thumb[data-file]').forEach(img=>{
    if(img.dataset.wired) return;
    img.dataset.wired='1';
    let tries=0;
    const tick=()=>{
      // SVG placeholder is tiny; real jpeg is larger — also retry on error
      const pending = !img.naturalWidth || img.naturalWidth < 40;
      if(!pending || tries>=40) return;
      tries++;
      const f=img.dataset.file;
      img.src=`/api/thumb?file=${encodeURIComponent(f)}&t=${Date.now()}`;
      setTimeout(tick, 1500 + tries*200);
    };
    img.addEventListener('load',()=>{
      if(img.naturalWidth && img.naturalWidth>=40) return;
      setTimeout(tick, 1200);
    });
    img.addEventListener('error',()=>setTimeout(tick, 1500));
  });
}

function canPlayHevc(){
  const v=document.createElement('video');
  return !!(v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"')||v.canPlayType('video/mp4; codecs="hev1.1.6.L93.B0"'));
}
function closePlayer(){
  const p=$('player');
  p.pause(); p.removeAttribute('src'); p.load();
  $('playerModal').classList.remove('open');
  $('playerModal').setAttribute('aria-hidden','true');
  $('playerStatus').textContent='';
  if(document.fullscreenElement) document.exitFullscreen().catch(()=>{});
}
function play(v){
  const modal=$('playerModal');
  modal.classList.add('open');
  modal.setAttribute('aria-hidden','false');
  $('playerTitle').textContent=`${v.title} · ${fmtDate(v.captured_at)}`;
  const hevc=v.codec==='hevc'||v.codec==='h265';
  const needTx=hevc && !canPlayHevc();
  $('playerStatus').textContent=needTx
    ? 'GoPro HEVC: convirtiendo a H.264 para el navegador (puede tardar unos segundos)…'
    : (hevc?'HEVC nativo':'');
  const p=$('player');
  const key=itemKey(v);
  p.src=`/api/stream?file=${encodeURIComponent(key)}${needTx?'&transcode=1':''}`;
  p.onerror=()=>{$('playerStatus').textContent='No se pudo reproducir este vídeo.';};
  p.onplaying=()=>{if(needTx)$('playerStatus').textContent='Reproduciendo vista H.264';};
  p.play().catch(err=>{$('playerStatus').textContent='Pulsa play en el vídeo ('+(err&&err.message||'autoplay bloqueado')+')';});
}
$('closePlayer').onclick=closePlayer;
$('playerModal').addEventListener('click',e=>{if(e.target===$('playerModal')) closePlayer();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('playerModal').classList.contains('open')) closePlayer();});
$('btnFs').onclick=()=>{
  const shell=$('playerShell');
  if(!document.fullscreenElement) shell.requestFullscreen?.().catch(()=>$('player').requestFullscreen?.());
  else document.exitFullscreen?.();
};
$('player').addEventListener('dblclick',()=>$('btnFs').click());
$('q').oninput=e=>{state.filter=e.target.value;clearTimeout(searchTimer);searchTimer=setTimeout(()=>load(),300);};
$('sortBy').onchange=e=>{state.sort=e.target.value;load();};
$('tabVideos').onclick=()=>{state.tab='video';state.folder='';state.selected.clear();$('tabVideos').classList.add('active');$('tabPhotos').classList.remove('active');load();};
$('tabPhotos').onclick=()=>{state.tab='photo';state.folder='';state.selected.clear();$('tabPhotos').classList.add('active');$('tabVideos').classList.remove('active');load();};
$('selAll').onclick=()=>{state.items.forEach(v=>state.selected.add(itemKey(v)));render()};
$('selNone').onclick=()=>{state.selected.clear();render()};

$('btnDelete').onclick=()=>{
  const n=state.selected.size;
  if(!n) return;
  // Una sola confirmación por selección (no por vídeo)
  $('dlgText').textContent=`Vas a eliminar ${n} vídeo${n===1?'':'s'} de forma permanente del disco. ¿Continuar?`;
  $('dlg').showModal();
};
$('dlgCancel').onclick=()=>$('dlg').close();
$('dlgOk').onclick=async()=>{
  const files=[...state.selected];
  $('dlg').close();
  $('btnDelete').disabled=true;
  try{
    const r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:state.tab==='photo'?'photo':'video',files})});
    const j=await r.json();
    if(!j.ok){ alert('Error: '+(j.error||r.status)); return; }
    state.selected.clear();
    await load();
  } finally { $('btnDelete').disabled=false; }
};

$('loginLink').addEventListener('click',e=>{if(session.auth_local){e.preventDefault();$('loginErr').textContent='';$('loginDlg').showModal();}});
$('loginCancel').onclick=()=>$('loginDlg').close();
$('loginForm').onsubmit=async e=>{
  e.preventDefault();
  $('loginErr').textContent='';
  const user=$('loginUser').value.trim();
  const pass=$('loginPass').value;
  const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:user,password:pass})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok||!j.ok){$('loginErr').textContent='Usuario o contraseña incorrectos';return;}
  $('loginDlg').close();
  await who();
  await load();
};
who().then(load);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "media-portal/0.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[media] {self.address_string()} {fmt % args}")

    def _user(self) -> dict | None:
        if AUTH_DISABLED:
            return _GUEST_USER
        cookies = parse_cookies(self.headers.get("Cookie", ""))
        email = verify(cookies.get(COOKIE))
        return user_for(email)

    def _ffmpeg_transcode_args(self) -> tuple[str, list[str]]:
        pref = os.environ.get("FFMPEG_VENC", "auto").strip().lower()
        preset = os.environ.get("FFMPEG_PRESET", "").strip()
        if pref in ("h264_nvenc", "nvenc"):
            return "h264_nvenc", ["-preset", preset or "p4"]
        if pref == "libx264":
            return "libx264", ["-preset", preset or "veryfast", "-crf", "23"]
        # auto: NVENC en HanSolo si ffmpeg lo expone; en Raspberry / Docker genérico → x264
        try:
            enc = subprocess.check_output(
                ["ffmpeg", "-hide_banner", "-encoders"],
                text=True,
                timeout=15,
            )
            if "h264_nvenc" in enc:
                return "h264_nvenc", ["-preset", preset or "p4"]
        except Exception:
            pass
        return "libx264", ["-preset", preset or "veryfast", "-crf", "23"]

    def _stream_transcode(self, video: Path, head_only: bool = False) -> None:
        venc, vopts = self._ffmpeg_transcode_args()
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(video),
            "-c:v",
            venc,
            *vopts,
            "-vf",
            "scale='min(1280,iw)':-2",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if not proc.stdout:
                return
            while True:
                chunk = proc.stdout.read(1024 * 64)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _html(self, code: int, html: str, extra: dict | None = None) -> None:
        self._send(code, html.encode(), "text/html; charset=utf-8", extra)

    def _json(self, code: int, obj: dict, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _redirect(self, loc: str, extra: dict | None = None) -> None:
        headers = {"Location": loc}
        if extra:
            headers.update(extra)
        self.send_response(302)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def _safe_media(self, kind: str, rel: str) -> Path | None:
        base = VIDEOS if kind == "video" else PHOTOS
        exts = VIDEO_EXTS if kind == "video" else PHOTO_EXTS
        p = _safe_rel_path(base, rel)
        if not p or not p.is_file() or p.suffix.lower() not in exts:
            return None
        return p

    def _safe_video(self, rel: str) -> Path | None:
        return self._safe_media("video", rel)

    def do_HEAD(self) -> None:  # noqa: N802
        # Avoid streaming body on HEAD (browsers probe video this way).
        self.close_connection = False
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path == "/auth/google/start":
            if AUTH_DISABLED or AUTH_LOCAL:
                self._redirect("/")
                return
            verifier = _b64url(secrets.token_bytes(32))
            challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
            state = _b64url(json.dumps({"v": verifier, "t": time.time()}).encode())
            with PKCE_LOCK:
                PKCE_STORE[state] = {"verifier": verifier, "t": time.time()}
            gu = urllib.parse.urlencode(
                {
                    "client_id": CLIENT_ID,
                    "response_type": "code",
                    "redirect_uri": CALLBACK_URL,
                    "scope": "openid email profile",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "access_type": "online",
                }
            )
            if qs.get("switch"):
                gu = urllib.parse.urlencode(
                    {
                        "client_id": CLIENT_ID,
                        "response_type": "code",
                        "redirect_uri": CALLBACK_URL,
                        "scope": "openid email profile",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": state,
                        "access_type": "online",
                        "prompt": "select_account",
                    }
                )
            self._redirect("https://accounts.google.com/o/oauth2/v2/auth?" + gu)
            return

        if path == "/auth/google/callback":
            if AUTH_DISABLED:
                self._redirect("/")
                return
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [""])[0]
            with PKCE_LOCK:
                st = PKCE_STORE.pop(state, None)
            if not st:
                st = pkce_from_oauth_state(state)
            if not code:
                err = (qs.get("error") or [""])[0]
                print(f"[media] oauth callback sin code error={err!r}", flush=True)
                self._redirect("/auth/google/start")
                return
            if not st:
                print("[media] oauth callback: PKCE state perdido o caducado -> nuevo login", flush=True)
                self._redirect("/auth/google/start")
                return
            try:
                tok = http_json(
                    "https://oauth2.googleapis.com/token",
                    {
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": CALLBACK_URL,
                        "code_verifier": st["verifier"],
                    },
                )
                if not tok.get("access_token"):
                    self._html(400, "<h1>Error al canjear el código</h1>")
                    return
                req = urllib.request.Request(
                    "https://www.googleapis.com/oauth2/v1/userinfo?alt=json",
                    headers={"Authorization": f"Bearer {tok['access_token']}"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    uinfo = json.loads(resp.read().decode())
                email = str(uinfo.get("email") or "").lower()
                u = user_for(email)
                if not u:
                    self._html(
                        403,
                        f"<h1>No autorizado</h1><p>{email} no está en la lista de acceso.</p>",
                    )
                    return
                self._redirect("/", {"Set-Cookie": _session_cookie(email)})
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    print("[media] oauth token 400 (código usado/caducado) -> nuevo login", flush=True)
                    self._redirect("/auth/google/start")
                else:
                    self._html(500, f"<h1>Error</h1><pre>{e}</pre>")
            except Exception as e:
                self._html(500, f"<h1>Error</h1><pre>{e}</pre>")
            return

        if path == "/__logout":
            if AUTH_DISABLED:
                self._redirect("/")
                return
            clear = _clear_session_cookie()
            if AUTH_LOCAL:
                self._redirect("/", {"Set-Cookie": clear})
            else:
                self._redirect("/auth/google/start?switch=1", {"Set-Cookie": clear})
            return

        if path == "/__whoami":
            if AUTH_DISABLED:
                self._json(
                    200,
                    {
                        "email": None,
                        "name": None,
                        "admin": True,
                        "auth_disabled": True,
                        "public_read": False,
                    },
                )
                return
            u = self._user()
            self._json(
                200,
                {
                    "email": u.get("email") if u else None,
                    "username": u.get("username") if u else None,
                    "name": u.get("name") if u else None,
                    "admin": bool(u.get("admin")) if u else False,
                    "auth_disabled": False,
                    "public_read": AUTH_PUBLIC_READ,
                    "auth_local": AUTH_LOCAL,
                },
            )
            return

        if not AUTH_DISABLED:
            u = self._user()
            if not u and not (AUTH_PUBLIC_READ and path in _PUBLIC_GET_PATHS):
                if AUTH_LOCAL:
                    self._redirect("/")
                else:
                    self._redirect("/auth/google/start")
                return

        if path in ("/", "/index.html"):
            self._html(200, _index_page())
            return

        if path == "/api/logo":
            logo = _portal_logo_path()
            if not logo:
                self.send_error(404)
                return
            mime = PHOTO_MIME.get(logo.suffix.lower(), "image/png")
            self._send(200, logo.read_bytes(), mime, {"Cache-Control": "public, max-age=3600"})
            return

        if path == "/api/videos":
            folder = (qs.get("folder") or [""])[0]
            sort = (qs.get("sort") or ["date_desc"])[0]
            search = (qs.get("q") or [""])[0]
            data = browse_media("video", folder, sort, search)
            self._json(200, {"ok": True, **data, "count": len(data["items"])})
            return

        if path == "/api/photos":
            folder = (qs.get("folder") or [""])[0]
            sort = (qs.get("sort") or ["date_desc"])[0]
            search = (qs.get("q") or [""])[0]
            data = browse_media("photo", folder, sort, search)
            self._json(200, {"ok": True, **data, "count": len(data["items"])})
            return

        if path == "/api/photo":
            rel = (qs.get("file") or [""])[0]
            photo = self._safe_media("photo", rel)
            if not photo:
                self.send_error(404)
                return
            mime = PHOTO_MIME.get(photo.suffix.lower(), "application/octet-stream")
            self._send(200, photo.read_bytes(), mime, {"Cache-Control": "public, max-age=3600"})
            return

        if path == "/api/thumb":
            try:
                rel = (qs.get("file") or [""])[0]
                kind = (qs.get("kind") or ["video"])[0].lower()
                wait = (qs.get("wait") or ["0"])[0] in ("1", "true", "yes")
                if kind == "photo":
                    photo = self._safe_media("photo", rel)
                    if not photo:
                        self.send_error(404)
                        return
                    thumb = ensure_photo_thumb(photo, rel, wait=wait)
                    if thumb and thumb.is_file():
                        mime = PHOTO_MIME.get(thumb.suffix.lower(), "image/jpeg")
                        data = thumb.read_bytes()
                        self._send(200, data, mime, {"Cache-Control": "public, max-age=86400"})
                        return
                else:
                    video = self._safe_video(rel)
                    if not video:
                        self.send_error(404)
                        return
                    thumb = ensure_thumb(video, wait=wait, rel=rel)
                    if thumb and thumb.is_file():
                        data = thumb.read_bytes()
                        self._send(200, data, "image/jpeg", {"Cache-Control": "public, max-age=86400"})
                        return
                self._send(
                    200,
                    _PLACEHOLDER_SVG,
                    "image/svg+xml",
                    {"Cache-Control": "no-store", "X-Thumb-Status": "pending"},
                )
            except Exception as e:
                print(f"[media] thumb handler error: {e}", flush=True)
                self._send(
                    200,
                    _PLACEHOLDER_SVG,
                    "image/svg+xml",
                    {"Cache-Control": "no-store", "X-Thumb-Status": "error"},
                )
            return

        if path == "/api/stream":
            rel = (qs.get("file") or [""])[0]
            video = self._safe_video(rel)
            if not video:
                self.send_error(404)
                return
            head_only = getattr(self, "_head_only", False)
            want_tx = (qs.get("transcode") or ["0"])[0] in ("1", "true", "yes")
            if want_tx:
                # Prefer cached H.264 proxy when present; else live NVENC pipe
                prox = proxy_path(video, rel)
                if prox.is_file() and prox.stat().st_mtime >= video.stat().st_mtime and prox.stat().st_size > 1024:
                    video = prox
                else:
                    self._stream_transcode(video, head_only=head_only)
                    return

            size = video.stat().st_size
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start_s, _, end_s = rng.replace("bytes=", "").partition("-")
                start = int(start_s or 0)
                end = int(end_s) if end_s else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if head_only:
                    return
                try:
                    with open(video, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if head_only:
                return
            try:
                with open(video, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path == "/auth/login":
            if AUTH_DISABLED or not AUTH_LOCAL:
                self._json(404, {"ok": False, "error": "not found"})
                return
            body = self._read_json()
            user = authenticate_local(str(body.get("username") or ""), str(body.get("password") or ""))
            if not user:
                self._json(401, {"ok": False, "error": "bad credentials"})
                return
            ident = session_ident(user)
            self._json(200, {"ok": True, "admin": bool(user.get("admin"))}, {"Set-Cookie": _session_cookie(ident)})
            return
        u = self._user()
        if not u:
            self._json(401, {"ok": False, "error": "auth"})
            return
        if AUTH_PUBLIC_READ and not u.get("admin"):
            self._json(403, {"ok": False, "error": "admin"})
            return
        if path == "/api/delete":
            body = self._read_json()
            files = body.get("files") or []
            kind = (body.get("kind") or "video").lower()
            if not isinstance(files, list) or not files:
                self._json(400, {"ok": False, "error": "empty"})
                return
            deleted = []
            errors = []
            for name in files:
                rel = str(name)
                p = self._safe_media("photo" if kind == "photo" else "video", rel)
                if not p:
                    errors.append(rel)
                    continue
                try:
                    mid = None
                    if kind == "video":
                        m = re.search(r"_([0-9a-f]{8})$", p.stem, re.I)
                        if m:
                            meta = load_library().get(m.group(1).lower()) or {}
                            mid = meta.get("id") or m.group(1).lower()
                    p.unlink()
                    tkey = _thumb_key(rel)
                    if kind == "photo":
                        pt = _photo_thumb_path(rel)
                        if pt.is_file():
                            pt.unlink(missing_ok=True)
                    else:
                        thumb = THUMBS / (tkey + ".jpg")
                        if thumb.is_file():
                            thumb.unlink(missing_ok=True)
                        for extra in (
                            THUMBS / (tkey + ".codec"),
                            proxy_path(p, rel),
                            Path(str(proxy_path(p, rel)) + ".part"),
                        ):
                            if extra.is_file():
                                extra.unlink(missing_ok=True)
                        mark_download_deleted(mid, p.name)
                    deleted.append(rel)
                except Exception as e:
                    errors.append(f"{rel}: {e}")
            self._json(200, {"ok": True, "deleted": deleted, "errors": errors})
            return
        self._json(404, {"ok": False, "error": "not found"})


def main() -> None:
    if AUTH_DISABLED:
        print("[media] AUTH_DISABLED=1 — portal sin Google OAuth", flush=True)
    elif AUTH_LOCAL and AUTH_PUBLIC_READ:
        print("[media] AUTH_LOCAL + AUTH_PUBLIC_READ — visitantes anónimos, admin usuario/clave", flush=True)
    elif AUTH_LOCAL:
        print("[media] AUTH_LOCAL=1 — login usuario/clave (sin Google)", flush=True)
    elif AUTH_PUBLIC_READ:
        print("[media] AUTH_PUBLIC_READ=1 — lectura pública, admin con OAuth", flush=True)
    elif not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET required")
    warm_thumbs_background()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"media-portal on http://{HOST}:{PORT} root={ROOT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
