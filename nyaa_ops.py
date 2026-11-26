"""بحث Nyaa المدمج + المفضلة (Watchlist) — ROUND 1 · WEIGH-IN.

يعتمد على Nyaa RSS العام بدون مفاتيح، مع كاش قصير لتفادي الـ rate-limit.
"""
import email.utils
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from media_ops import (
    NYAA_VIEW_RE,
    clean_torrent_title,
    is_batch_torrent,
    normalize_nyaa_link,
)

_RSS_URL = "https://nyaa.si/?page=rss&q={q}&c=1_2&f=0"
_NYAA_NS = "{https://nyaa.si/xmlns/nyaa}"
_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
CACHE_TTL_SEC = 240  # 4 دقائق
_WATCHLIST_PATH = Path("/workspace/watchlist.json")
_TEAM_RE = re.compile(r"^\[([^\]]+)\]")
_UA = {"User-Agent": "runpod-anime-subber-single"}


def _fetch_rss(query: str, log_cb: Optional[Callable[[str], None]] = None) -> str:
    url = _RSS_URL.format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def team_badge(title: str) -> str:
    m = _TEAM_RE.match((title or "").strip())
    return m.group(1) if m else "—"


def nyaa_search(query: str, log_cb: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """بحث في Nyaa RSS. النتائج مرتبة بالسيدرز: {team,title,size_bytes,size_h,seeders,view,download,pub_ts,is_batch}."""
    q = (query or "").strip()
    if not q:
        return []
    hit = _CACHE.get(q.lower())
    if hit and (time.time() - hit[0]) < CACHE_TTL_SEC:
        if log_cb:
            log_cb("⚡ نتائج Nyaa من الكاش (أقل من 4 دقائق).")
        return hit[1]
    xml_text = _fetch_rss(q)
    root = ET.fromstring(xml_text)
    results: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        view = (item.findtext("link") or item.findtext("guid") or "").strip()
        # nyaa:size تأتي بصيغة "1.4 GiB"
        size_b = 0
        size_txt = item.findtext(f"{_NYAA_NS}size") or ""
        m = re.match(r"([\d.]+)\s*(KiB|MiB|GiB|TiB)", size_txt)
        if m:
            mult = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}[m.group(2)]
            size_b = int(float(m.group(1)) * mult)
        try:
            seeders = int(item.findtext(f"{_NYAA_NS}seeders") or "0")
        except ValueError:
            seeders = 0
        pub_ts = 0.0
        pub = item.findtext("pubDate")
        if pub:
            try:
                pub_ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                pub_ts = 0.0
        if not view:
            continue
        results.append({
            "team": team_badge(title),
            "title": title,
            "size_bytes": size_b,
            "size_h": size_txt or f"{size_b / 1024**2:.0f} MiB",
            "seeders": seeders,
            "view": view,
            "download": normalize_nyaa_link(view),
            "pub_ts": pub_ts,
            "is_batch": is_batch_torrent(title, size_b),
        })
    results.sort(key=lambda r: r["seeders"], reverse=True)
    _CACHE[q.lower()] = (time.time(), results)
    if log_cb:
        log_cb(f"🔎 Nyaa: {len(results)} نتيجة لـ \"{q}\" (مرتبة بالسيدرز).")
    return results


def load_watchlist() -> List[str]:
    try:
        if _WATCHLIST_PATH.is_file():
            data = json.loads(_WATCHLIST_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_watchlist(series: List[str], log_cb: Optional[Callable[[str], None]] = None) -> None:
    _WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WATCHLIST_PATH.write_text(json.dumps(series, ensure_ascii=False, indent=2), encoding="utf-8")
    # مزامنة على Drive (best-effort)
    try:
        from media_ops import rclone_remote_configured, upload_to_rclone
        if rclone_remote_configured("gdrive"):
            upload_to_rclone(str(_WATCHLIST_PATH), "gdrive", "Runpod_Anime", log_cb=log_cb)
            if log_cb:
                log_cb("☁️ تمت مزامنة المفضلة على Drive.")
    except Exception as exc:
        if log_cb:
            log_cb(f"ℹ️ المفضلة محفوظة محلياً فقط (تعذرت مزامنة Drive: {exc}).")


def check_new_today(series_list: List[str], log_cb: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """أحدث حلقات أنميات المفضلة خلال آخر 24 ساعة، مرتبة بالأحدث."""
    cutoff = time.time() - 24 * 3600
    found: List[Dict[str, Any]] = []
    for name in series_list:
        try:
            for r in nyaa_search(name, log_cb=None):
                if r["pub_ts"] >= cutoff and not r["is_batch"]:
                    r = dict(r)
                    r["series"] = name
                    found.append(r)
        except Exception as exc:
            if log_cb:
                log_cb(f"⚠️ تعذر فحص \"{name}\": {exc}")
    found.sort(key=lambda r: r["pub_ts"], reverse=True)
    return found


def guess_series_from_title(title: str, saved: List[Tuple[str, str]]) -> Tuple[str, Optional[str]]:
    """استخراج اسم الأنمي من عنوان التورنت + مطابقة مع المسلسلات المحفوظة.

    saved: قائمة list_saved_series() = [(label, value)].
    يرجع (الاسم_المستخرج, القيمة_المحفوظة_المطابقة أو None).
    """
    info = clean_torrent_title(title or "")
    guess = (info.get("series") or "").strip()
    gl = guess.lower()
    best: Optional[str] = None
    for label, value in saved:
        vl = (value or label).lower()
        if vl and (vl in gl or gl in vl):
            best = value
            break
    return guess, best
