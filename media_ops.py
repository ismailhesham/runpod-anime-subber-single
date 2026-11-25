import base64
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts"}

REQUIRED_FONT_FILES = [
    "AFSANEH_v2 KHWF.otf",
    "Bahij Nassim Bold.ttf",
    "Bahij_Mitra-Regular.ttf",
    "BKR Shahd Extra Bold.ttf",
    "Hayah.otf",
    "Loving Stay.otf",
    "Sovba_Regular.otf",
]


ProgressCallback = Callable[[float, str, str], None]



def verify_required_fonts(fonts_dir: str) -> None:
    """Strictly verify all 7 custom fonts exist in fonts/ so libass never substitutes random fonts."""
    fdir = Path(fonts_dir)
    missing = [f for f in REQUIRED_FONT_FILES if not (fdir / f).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing required font file(s) in {fdir}: {missing}. "
            "Make sure the fonts/ folder was uploaded to GitHub and cloned completely."
        )


def probe_video_info(video_path: str) -> Tuple[int, int, float, float]:
    """Return (width, height, fps, duration_seconds) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json",
        video_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or [{}]
    st = streams[0]
    w = int(st.get("width") or 1920)
    h = int(st.get("height") or 1080)
    fps_raw = str(st.get("r_frame_rate") or "24000/1001")
    try:
        if "/" in fps_raw:
            num, den = fps_raw.split("/", 1)
            fps = float(num) / max(1.0, float(den))
        else:
            fps = float(fps_raw)
    except Exception:
        fps = 23.976
    dur = 0.0
    try:
        dur = float((data.get("format") or {}).get("duration") or 0.0)
    except Exception:
        pass
    return w, h, max(1.0, fps), max(0.0, dur)


_EP_NUM_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})"),
    re.compile(r"(?:^|[\s_\-\[\(.])(?:ep|episode|e)[\s_.\-]*(\d{1,4})(?:v\d+)?(?:[\s_\-\]\)\.\[]|$)", re.IGNORECASE),
    re.compile(r"-\s*(\d{1,4})(?:v\d+)?\s*[\[\.\-]"),
    re.compile(r"(?:^|[\s_\-\[\(])(\d{2,4})(?:v\d+)?(?:[\s_\-\]\)\.\[]|$)"),
]

_RESOLUTION_NUMBERS = {360, 480, 540, 576, 720, 900, 1080, 1440, 2160}


def extract_episode_number(name: str) -> Optional[int]:
    """Best-effort episode number from a fansub filename (S01E05, 'Name - 05', 'Ep 05'...)."""
    stem = Path(name).stem
    m = _EP_NUM_PATTERNS[0].search(stem)
    if m:
        return int(m.group(2))
    for pat in _EP_NUM_PATTERNS[1:]:
        for mm in pat.finditer(stem):
            num = int(mm.group(1))
            if num in _RESOLUTION_NUMBERS:
                continue
            return num
    return None


def episode_sort_key(path) -> Tuple[int, str]:
    p = Path(path)
    num = extract_episode_number(p.name)
    return (num if num is not None else 99999, p.name.lower())


def collect_episode_videos(folder) -> List[Path]:
    """All fully downloaded videos inside a season folder, sorted in episode order."""
    folder = Path(folder)
    if not folder.exists():
        return []
    if any(folder.rglob("*.aria2")):
        return []
    vids = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and p.stat().st_size > 1024 * 1024
    ]
    vids.sort(key=episode_sort_key)
    return vids


def find_completed_video(folder: Path) -> Optional[Path]:
    """Return completed video file (>1MB and no active .aria2 control file) if already downloaded."""
    if not folder.exists():
        return None
    if any(folder.rglob("*.aria2")):
        return None
    videos = sorted(
        [
            p for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and p.stat().st_size > 1024 * 1024
        ],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return videos[0] if videos else None


def download_torrent_or_url(
    source_link: Optional[str],
    torrent_file: Optional[str],
    output_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Path:
    """Download or resume via aria2c into persistent storage, skipping immediately if already cached."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if torrent_file and os.path.isfile(torrent_file) and Path(torrent_file).suffix.lower() in VIDEO_EXTENSIONS:
        vp = Path(torrent_file)
        mb = vp.stat().st_size / (1024 * 1024)
        if log_cb:
            log_cb(f"⚡ استخدام ملف الفيديو المحفوظ مباشرة ({mb:.1f} MB): {vp.name}")
        if progress_cb:
            progress_cb(100.0, "جاهز محلياً بدون تحميل ⚡", f"{mb:.1f} MB · {vp.name}")
        return vp

    cached = find_completed_video(out_path)
    if cached is not None:
        mb = cached.stat().st_size / (1024 * 1024)
        if log_cb:
            log_cb(f"⚡ الحلقة محفوظة مسبقاً بالكامل ({mb:.1f} MB): {cached.name} — تم تخطي التحميل!")
        if progress_cb:
            progress_cb(100.0, "محفوظة مسبقاً على السيرفر ⚡", f"{mb:.1f} MB · {cached.name}")
        return cached

    target = None
    if torrent_file and os.path.isfile(torrent_file):
        target = torrent_file
    elif source_link and source_link.strip():
        target = source_link.strip()

    if not target:
        raise ValueError("لم يتم إدخال رابط أو اختيار حلقة محفوظة.")

    _run_aria2(target, output_dir, log_cb=log_cb, progress_cb=progress_cb)

    videos = sorted(
        [p for p in out_path.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not videos:
        raise FileNotFoundError(f"لم يتم العثور على ملف فيديو داخل {output_dir}")

    mb = videos[0].stat().st_size / (1024 * 1024)
    if progress_cb:
        progress_cb(100.0, "اكتمل التحميل 100% وحُفظ دائماً ✓", f"{mb:.1f} MB · {videos[0].name}")
    return videos[0]


def _load_bt_trackers(log_cb: Optional[Callable[[str], None]] = None) -> str:
    """Best-effort public tracker list for magnet/torrent swarms.

    Order: fresh pod cache (/workspace/trackers.txt, 7 days) -> live fetch from
    ngosang/trackerslist -> list shipped in the repo (trackers.txt). Returns a
    comma-joined tracker string for aria2's --bt-tracker, or "" on total failure.
    """
    cache = Path("/workspace/trackers.txt")
    try:
        if cache.is_file() and (time.time() - cache.stat().st_mtime) < 7 * 86400:
            txt = cache.read_text(encoding="utf-8").strip()
            if txt:
                return ",".join(txt.split())
    except OSError:
        pass
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
            headers={"User-Agent": "runpod-anime-subber"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            txt = resp.read().decode("utf-8", "replace").strip()
        if txt:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(txt, encoding="utf-8")
            except OSError:
                pass
            if log_cb:
                log_cb(f"🧲 تم تحديث قائمة الـ trackers ({len(txt.split())} متتبعاً).")
            return ",".join(txt.split())
    except Exception as exc:
        if log_cb:
            log_cb(f"ℹ️ تعذر تحديث قائمة الـ trackers ({exc}) — استخدام النسخة المرفقة.")
    repo_list = Path(__file__).resolve().parent / "trackers.txt"
    try:
        if repo_list.is_file():
            txt = repo_list.read_text(encoding="utf-8").strip()
            if txt:
                return ",".join(txt.split())
    except OSError:
        pass
    return ""


def list_torrent_files(
    source_link: Optional[str],
    torrent_file: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    timeout: int = 180,
) -> List[Tuple[int, str, int]]:
    """List files inside a magnet / .torrent (path or URL) WITHOUT downloading content.

    Returns [(1-based index, relative path, size bytes)] in the torrent's own order.
    For magnets, aria2c first fetches metadata only (--bt-metadata-only) into a temp dir.
    """
    tmpdir: Optional[str] = None
    try:
        target_path: Optional[str] = None
        if torrent_file and os.path.isfile(torrent_file) and Path(torrent_file).suffix.lower() == ".torrent":
            target_path = torrent_file
        else:
            link = (source_link or "").strip()
            if not link:
                raise ValueError("لم يتم إدخال رابط تورنت.")
            if os.path.isfile(link) and Path(link).suffix.lower() == ".torrent":
                target_path = link
            elif link.lower().startswith("magnet:"):
                tmpdir = tempfile.mkdtemp(prefix="torr_meta_")
                cmd = [
                    "aria2c", "--dir", tmpdir,
                    "--bt-metadata-only=true", "--bt-save-metadata=true",
                    "--seed-time=0", "--bt-stop-timeout=90",
                    "--max-connection-per-server=8", "--summary-interval=0",
                    "--console-log-level=warn",
                    link,
                ]
                bt_trackers = _load_bt_trackers()
                if bt_trackers:
                    cmd[2:2] = [
                        f"--bt-tracker={bt_trackers}",
                        "--bt-tracker-connect-timeout=10",
                        "--bt-tracker-timeout=10",
                    ]
                if log_cb:
                    log_cb("🧲 جاري سحب بيانات التورنت (metadata) لعرض الملفات بدون تحميل...")
                subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                torr = sorted(Path(tmpdir).glob("*.torrent"))
                if not torr:
                    raise RuntimeError("تعذر سحب بيانات التورنت خلال المهلة — الرابط قد يكون ميتاً أو بدون سيدرز كافيين.")
                target_path = str(torr[0])
            elif link.lower().split("?")[0].endswith(".torrent"):
                tmpdir = tempfile.mkdtemp(prefix="torr_meta_")
                req = urllib.request.Request(link, headers={"User-Agent": "runpod-anime-subber"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                target_path = str(Path(tmpdir) / "link.torrent")
                Path(target_path).write_bytes(data)
            else:
                raise ValueError("عرض الملفات متاح لروابط التورنت/الماجنت فقط.")

        res = subprocess.run(
            ["aria2c", "--show-files=true", target_path],
            capture_output=True, text=True, timeout=60,
        )
        files: List[Tuple[int, str, int]] = []
        cur_idx: Optional[int] = None
        cur_path = ""
        for line in (res.stdout or "").splitlines():
            m = re.match(r"^\s*(\d+)\|(.*)$", line)
            if m:
                cur_idx = int(m.group(1))
                cur_path = m.group(2).strip()
                if cur_path.startswith("./"):
                    cur_path = cur_path[2:]
                continue
            m2 = re.match(r"^\s+\|[^|]*\(([\d,]+)\)", line)
            if m2 and cur_idx is not None:
                files.append((cur_idx, cur_path, int(m2.group(1).replace(",", ""))))
                cur_idx = None
        if not files:
            raise RuntimeError("لم يتم التعرف على ملفات داخل هذا التورنت.")
        return files
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _run_aria2(
    target: str,
    output_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
    select_files: Optional[List[int]] = None,
) -> None:
    """Run aria2c (torrent/magnet/URL) into output_dir with resume support and live progress."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    has_partial = any(out_path.rglob("*.aria2"))
    overwrite_flag = "--allow-overwrite=false" if has_partial else "--allow-overwrite=true"

    cmd = [
        "aria2c",
        "--dir", output_dir,
        "--continue=true",
        overwrite_flag,
        "--auto-file-renaming=false",
        "--seed-time=0",
        "--bt-stop-timeout=300",
        "--max-connection-per-server=8",
        "--split=8",
        "--file-allocation=none",
        "--summary-interval=1",
        target,
    ]
    if select_files:
        cmd[-1:-1] = [
            f"--select-file={','.join(str(i) for i in select_files)}",
        ]
    bt_trackers = _load_bt_trackers(log_cb=log_cb)
    if bt_trackers:
        cmd[-1:-1] = [
            f"--bt-tracker={bt_trackers}",
            "--bt-tracker-connect-timeout=10",
            "--bt-tracker-timeout=10",
        ]
    if log_cb:
        mode_txt = "استكمال التحميل السابق" if has_partial else "بدء التحميل وحفظه دائماً"
        if select_files:
            mode_txt += f" — تحميل انتقائي ({len(select_files)} ملفاً فقط من التورنت)"
        log_cb(f"[aria2c] {mode_txt} في: {output_dir}")
    if progress_cb:
        progress_cb(1.0, "جاري الاتصال بالمصدر وجلب البيانات...", output_dir)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None

    last_log_t = 0.0
    for line in proc.stdout:
        clean = line.strip()
        if not clean:
            continue
        pct_m = re.search(r"\((\d+(?:\.\d+)?)%\)", clean)
        if pct_m:
            pct = float(pct_m.group(1))
            dl_m = re.search(r"DL:([^\s\]]+)", clean)
            eta_m = re.search(r"ETA:([^\s\]]+)", clean)
            sz_m = re.search(r"([\d.]+\w*B/[\d.]+\w*B)", clean)
            dl_spd = dl_m.group(1) if dl_m else "—"
            eta_str = eta_m.group(1) if eta_m else "—"
            sz_str = sz_m.group(1) if sz_m else ""
            if progress_cb:
                progress_cb(pct, f"السرعة: {dl_spd}/s · المتبقي: {eta_str}", sz_str or "جاري التحميل...")
            now_t = time.monotonic()
            if log_cb and (now_t - last_log_t >= 6.0):
                last_log_t = now_t
                log_cb(f"[aria2c] {pct:.0f}% | السرعة: {dl_spd}/s | الحجم: {sz_str} | المتبقي: {eta_str}")
        elif "METADATA" in clean.upper() and progress_cb:
            progress_cb(2.0, "جاري سحب بيانات التورنت (Metadata)...", "الاتصال بالـ Seeders")
        elif log_cb and any(k in clean for k in ("Download complete", "ERROR")):
            log_cb(f"[aria2c] {clean}")

    if proc.wait() != 0:
        raise RuntimeError("فشل تحميل aria2c — تأكد من صحة رابط التورنت أو الرابط المباشر.")


def _download_selected_files(
    source_link: Optional[str],
    torrent_file: Optional[str],
    out_path: Path,
    wanted: List[int],
    log_cb: Optional[Callable[[str], None]],
    progress_cb: Optional[ProgressCallback],
) -> List[Path]:
    """Download only the chosen torrent file indices (1-based, same as --show-files / --select-file).

    Files already fully downloaded are kept and skipped, so he can come back later and
    pick more episodes from the same torrent without re-downloading anything.
    """
    listing = list_torrent_files(source_link, torrent_file, log_cb=log_cb)
    by_idx = {i: (p, s) for i, p, s in listing}
    missing_meta = [i for i in wanted if i not in by_idx]
    if missing_meta:
        raise ValueError(f"أرقام ملفات غير موجودة داخل التورنت: {missing_meta}")

    present: List[Path] = []
    to_fetch: List[int] = []
    for i in wanted:
        rel, _size = by_idx[i]
        fp = out_path / rel
        ctrl = Path(str(fp) + ".aria2")
        if fp.is_file() and fp.stat().st_size > 1024 * 1024 and not ctrl.exists():
            present.append(fp)
        else:
            to_fetch.append(i)

    if log_cb:
        log_cb(f"🎯 تحميل انتقائي: اخترت {len(wanted)} ملفاً — {len(present)} محفوظ مسبقاً، {len(to_fetch)} سيُحمَّل الآن.")
        for i in to_fetch:
            mb = by_idx[i][1] / (1024 * 1024)
            log_cb(f"   ⬇ {by_idx[i][0]} ({mb:.0f} MB)")

    if to_fetch:
        target = None
        if torrent_file and os.path.isfile(torrent_file):
            target = torrent_file
        elif source_link and source_link.strip():
            target = source_link.strip()
        if not target:
            raise ValueError("لم يتم إدخال رابط تورنت للموسم.")
        _run_aria2(target, str(out_path), log_cb=log_cb, progress_cb=progress_cb, select_files=to_fetch)
        for i in to_fetch:
            rel, _size = by_idx[i]
            fp = out_path / rel
            ctrl = Path(str(fp) + ".aria2")
            if fp.is_file() and fp.stat().st_size > 1024 * 1024 and not ctrl.exists():
                present.append(fp)

    if not present:
        raise FileNotFoundError("لم يكتمل تحميل أي من الملفات المنتقاة — راجع السجل.")
    missing = [i for i in wanted if not any((out_path / by_idx[i][0]) == p for p in present)]
    if missing and log_cb:
        names = ", ".join(Path(by_idx[i][0]).name for i in missing)
        log_cb(f"⚠️ ملفات لم تكتمل بعد (قد تحتاج إعادة تشغيل لاستكمالها): {names}")
    present.sort(key=episode_sort_key)
    return present


def download_season_batch(
    source_link: Optional[str],
    torrent_file: Optional[str],
    output_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
    select_files: Optional[List[int]] = None,
) -> List[Path]:
    """Download a whole season/batch torrent and return every episode video in episode order.

    A link may contain one episode or several seasons; all videos found are returned,
    sorted by the episode number extracted from each filename. Reuses the completed
    download on later runs (resume-safe). Pass select_files (1-based torrent indices)
    to download only those episodes; everything else in the torrent is left untouched.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if select_files:
        return _download_selected_files(
            source_link, torrent_file, out_path, sorted(set(select_files)), log_cb, progress_cb
        )

    cached = collect_episode_videos(out_path)
    if cached:
        total_mb = sum(p.stat().st_size for p in cached) / (1024 * 1024)
        if log_cb:
            log_cb(f"⚡ الموسم محفوظ مسبقاً بالكامل ({len(cached)} ملف فيديو، {total_mb:.0f} MB) — تم تخطي التحميل!")
        if progress_cb:
            progress_cb(100.0, "محفوظ مسبقاً على السيرفر ⚡", f"{len(cached)} حلقة · {total_mb:.0f} MB")
        return cached

    target = None
    if torrent_file and os.path.isfile(torrent_file):
        target = torrent_file
    elif source_link and source_link.strip():
        target = source_link.strip()
    if not target:
        raise ValueError("لم يتم إدخال رابط تورنت للموسم.")

    _run_aria2(target, output_dir, log_cb=log_cb, progress_cb=progress_cb)

    vids = collect_episode_videos(out_path)
    if not vids:
        raise FileNotFoundError(f"لم يتم العثور على ملفات فيديو داخل {output_dir}")
    if log_cb:
        log_cb(f"✓ اكتمل تحميل الدفعة: {len(vids)} حلقة بالترتيب:")
        for v in vids:
            ep = extract_episode_number(v.name)
            log_cb(f"   • حلقة {ep if ep is not None else '?'} ← {v.name}")
    if progress_cb:
        progress_cb(100.0, "اكتمل تحميل الدفعة 100% ✓", f"{len(vids)} حلقة")
    return vids


def _escape_ffmpeg_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


_NVENC_CAPS: Dict[str, set] = {}


def _has_nvenc(codec: str) -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return codec in (res.stdout or "")


def _nvenc_opts(codec: str) -> set:
    """Return the set of AVOptions this ffmpeg build's NVENC encoder supports (for safe flag gating)."""
    if codec not in _NVENC_CAPS:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", f"encoder={codec}"],
            capture_output=True, text=True,
        )
        _NVENC_CAPS[codec] = set((res.stdout or "").split())
    return _NVENC_CAPS[codec]


def _parse_hms(val: str) -> float:
    try:
        parts = val.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        pass
    return 0.0


def probe_audio_codec(video_path: str) -> str:
    """اسم كوديك أول تراك صوتي ('' لو مفيش صوت أو فشل الفحص)."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        )
        out = (res.stdout or "").strip()
        return out.splitlines()[0].strip().lower() if out else ""
    except Exception:
        return ""


def extract_card_frame(video_path: str, frac: float = 0.5, width: int = 480) -> str:
    """إطار JPEG واحد (base64) من الفيديو عند نسبة frac من مدته — لصور كروت التقدم.
    بيرجع '' لو الفشل لأي سبب (مفيش فشل صامت في اللوج هنا لأن الاستدعاء متكرر)."""
    try:
        _, _, _, dur = probe_video_info(video_path)
        t = max(0.5, float(dur or 0.0) * max(0.02, min(0.98, float(frac))))
        res = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", video_path,
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=30,
        )
        if res.returncode == 0 and res.stdout:
            return base64.b64encode(res.stdout).decode("ascii")
    except Exception:
        pass
    return ""


def burn_and_compress_native(
    video_path: str,
    ass_path: str,
    output_path: str,
    fonts_dir: str,
    codec: str = "h264_nvenc",
    cq: int = 23,
    preset: str = "p6",
    x265_preset: str = "slow",
    audio_bitrate: str = "160k",
    logo_path: str = "",
    logo_position: str = "top-right",
    logo_opacity: float = 0.30,
    logo_scale: float = 0.12,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """Single-pass NVENC burn: responsive ASS subtitles burned + compressed to MP4.
    When logo_path exists, the logo is overlaid (scaled, opacity-adjusted) in the chosen
    corner, subtitles burned on top. Missing logo = clean no-op."""
    if ass_path:
        verify_required_fonts(fonts_dir)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ass_filter = f"ass='{_escape_ffmpeg_path(os.path.abspath(ass_path))}':fontsdir='{_escape_ffmpeg_path(os.path.abspath(fonts_dir))}'"

    requested = codec
    if requested == "libx265" and log_cb:
        log_cb("وضع أصغر حجم: x265 على المعالج (preset slow) — أبطأ بكثير من كرت الشاشة.")
    if requested == "hevc_nvenc" and log_cb:
        log_cb("🎞️ وضع HEVC 10-bit (Main10): أعلى جودة في أقل حجم — بيشتغل على أي جهاز بيفك HEVC.")

    # الصوت: نسخ كما هو (بدون إعادة ضغط) لو متوافق مع MP4، وإلا AAC مع سطر في اللوج
    MP4_SAFE_AUDIO = {"aac", "mp3", "mp2", "ac3", "eac3", "opus", "flac", "alac"}
    if (audio_bitrate or "").strip().lower() == "copy":
        src_audio = probe_audio_codec(video_path)
        if src_audio and src_audio not in MP4_SAFE_AUDIO:
            if log_cb:
                log_cb(f"⚠️ الصوت الأصلي ({src_audio}) مش مضمون داخل MP4 — هيتم تحويله AAC 160k بدل النسخ.")
            a_args = ["-c:a", "aac", "-b:a", "160k"]
        else:
            a_args = ["-c:a", "copy"]
            if log_cb:
                log_cb(f"🎵 الصوت هيتنسخ زي ما هو بدون إعادة ضغط{f' ({src_audio})' if src_audio else ''}.")
    else:
        a_args = ["-c:a", "aac", "-b:a", audio_bitrate]

    def _build_v_args(eng: str) -> List[str]:
        if eng.endswith("_nvenc"):
            hevc10 = eng == "hevc_nvenc"
            v = [
                "-c:v", eng, "-preset", preset, "-rc", "vbr",
                "-cq", str(cq), "-b:v", "0",
                "-spatial-aq", "1", "-temporal-aq", "1",
                "-pix_fmt", "p010le" if hevc10 else "yuv420p",
            ]
            caps = _nvenc_opts(eng)
            if "-multipass" in caps:
                v += ["-multipass", "fullres"]  # better quality at the same size
            if "-bf" in caps:
                v += ["-bf", "3"]  # B-frames shrink anime encodes noticeably
            if "-rc-lookahead" in caps:
                v += ["-rc-lookahead", "20"]  # lookahead = noticeably better quality-per-bit
            if "-tune" in caps:
                v += ["-tune", "hq"]
            if eng == "hevc_nvenc":
                v += ["-profile:v", "main10", "-tag:v", "hvc1"]
            return v
        if eng == "libx265":
            # بلا أي كاب على الأنظمة: x265 بيبني pool بكل أنوية المعالج تلقائياً
            return ["-c:v", "libx265", "-preset", x265_preset, "-crf", str(cq), "-pix_fmt", "yuv420p",
                    "-x265-params", "aq-mode=3", "-tag:v", "hvc1"]
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(cq), "-pix_fmt", "yuv420p"]

    # سلسلة البدائل: المحرك المطلوب أولاً، ثم h264 على الكرت، ثم المعالج.
    # كرت بدون hevc_nvenc (أو محاولة GPU فشلت أثناء التشغيل) يكمل تلقائياً، وكل خطوة بتتسجل في اللوج.
    if requested.endswith("_nvenc"):
        chain = [requested]
        if requested != "h264_nvenc":
            chain.append("h264_nvenc")
        chain.append("libx265" if ("hevc" in requested or "x265" in requested) else "libx264")
    else:
        chain = [requested]
    attempts: List[str] = []
    for eng in chain:
        if eng.endswith("_nvenc") and not _has_nvenc(eng):
            if log_cb:
                log_cb(f"⚠️ نسخة ffmpeg على الجهاز ده من غير {eng} — هتخطاه وأجرب البديل.")
            continue
        attempts.append(eng)
    if not attempts:
        attempts = ["libx264"]

    vw_, vh_, _, total_dur = probe_video_info(video_path)

    use_logo = bool(logo_path and os.path.isfile(logo_path))
    if use_logo:
        op = min(1.0, max(0.05, float(logo_opacity)))
        sc = min(0.5, max(0.03, float(logo_scale)))
        logo_w = max(16, round(vw_ * sc)) if vw_ > 0 else 160
        margin = max(8, round(vw_ * 0.018)) if vw_ > 0 else 24
        pos = (logo_position or "top-right").strip().lower()
        x_expr = str(margin) if "left" in pos else f"W-w-{margin}"
        y_expr = str(margin) if "top" in pos else f"H-h-{margin}"
        fc = (
            f"[1:v]format=rgba,scale={logo_w}:-1,colorchannelmixer=aa={op:.2f}[logo];"
            f"[0:v][logo]overlay={x_expr}:{y_expr}[vt];"
            f"[vt]{ass_filter}[vout]"
        )
        if log_cb:
            log_cb(
                f"[logo] حرق اللوجو ({Path(logo_path).name}): الركن {pos}"
                f" · شفافية {int(op * 100)}% · عرض {logo_w}px"
            )

    PROGRESS_KEYS = (
        "frame=", "fps=", "stream_", "bitrate=", "total_size=",
        "out_time", "dup_frames=", "drop_frames=", "speed=", "progress=",
    )
    last_tail: List[str] = []
    ok = False
    for attempt_i, eng in enumerate(attempts):
        v_args = _build_v_args(eng)
        if (eng != requested or attempt_i > 0) and log_cb:
            log_cb(f"🔄 المحرك المطلوب ({requested}) مش متاح أو فشل — هكمل بـ {eng}.")
        if use_logo:
            cmd = [
                "ffmpeg", "-y", "-i", video_path, "-i", logo_path,
                "-filter_complex", fc,
                "-map", "[vout]", "-map", "0:a?", *v_args,
                *a_args,
                "-movflags", "+faststart",
                "-progress", "pipe:1",
                output_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", video_path,
                *(["-vf", ass_filter] if ass_path else []), *v_args,
                *a_args,
                "-movflags", "+faststart",
                "-progress", "pipe:1",
                output_path,
            ]

        if log_cb:
            log_cb(f"[ffmpeg] بدء الترميز لـ {Path(output_path).name} ({v_args[1]})...")
        if progress_cb:
            progress_cb(0.5, f"تشغيل محرك {v_args[1]}...", Path(output_path).name)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None

        tail: List[str] = []
        pstate: Dict[str, str] = {}
        last_log_t = 0.0

        for line in proc.stdout:
            clean = line.strip()
            if not clean:
                continue
            if clean.startswith(PROGRESS_KEYS) and "=" in clean:
                k, v = clean.split("=", 1)
                pstate[k.strip()] = v.strip()
                if k.strip() == "progress":
                    cur_sec = _parse_hms(pstate.get("out_time", "00:00:00"))
                    fps_v = pstate.get("fps", "0")
                    spd_v = pstate.get("speed", "—")
                    sz_mb = int(pstate.get("total_size", "0") or "0") / (1024 * 1024)
                    pct = min(99.8, (cur_sec / total_dur) * 100.0) if total_dur > 0 else 50.0
                    spd_num = 0.0
                    try:
                        spd_num = float(spd_v.replace("x", "").strip())
                    except Exception:
                        pass
                    eta_txt = "—"
                    if total_dur > 0 and spd_num > 0.05:
                        rem = max(0, int((total_dur - cur_sec) / spd_num))
                        eta_txt = f"{rem // 60:02d}:{rem % 60:02d}"
                    cm, cs = divmod(int(cur_sec), 60)
                    tm, ts = divmod(int(total_dur), 60)
                    if progress_cb:
                        progress_cb(
                            pct,
                            f"السرعة: {spd_v} · {fps_v} fps · المتبقي: {eta_txt}",
                            f"{cm:02d}:{cs:02d} / {tm:02d}:{ts:02d} · {sz_mb:.1f} MB",
                        )
                    now_t = time.monotonic()
                    if log_cb and (now_t - last_log_t >= 8.0):
                        last_log_t = now_t
                        log_cb(f"[ffmpeg] {pct:.1f}% | السرعة: {spd_v} | الحجم: {sz_mb:.1f} MB | المتبقي: {eta_txt}")
            else:
                tail.append(clean)
                if len(tail) > 25:
                    tail.pop(0)

        if proc.wait() == 0:
            ok = True
            break
        last_tail = tail
        if log_cb:
            log_cb(f"⚠️ محاولة {eng} فشلت (كود {proc.returncode}). آخر سطور ffmpeg:")
            for tl in tail[-6:]:
                log_cb(f"    {tl}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

    if not ok:
        raise RuntimeError("FFmpeg failed:\n" + "\n".join(last_tail[-10:]))

    orig_mb = os.path.getsize(video_path) / (1024 * 1024)
    new_mb = os.path.getsize(output_path) / (1024 * 1024)
    if progress_cb:
        progress_cb(100.0, "اكتمل الترميز 100% ✓", f"{orig_mb:.1f} MB → {new_mb:.1f} MB")
    if log_cb:
        log_cb(f"[ffmpeg] اكتمل بنجاح: {orig_mb:.1f} MB -> {new_mb:.1f} MB ({output_path})")
    return output_path



def burn_then_optional_compress(
    video_path: str, ass_path: str, output_path: str, fonts_dir: str,
    codec: str = "h264_nvenc", cq: int = 23, preset: str = "p6",
    x265_preset: str = "slow", audio_bitrate: str = "160k",
    logo_path: str = "", logo_position: str = "top-right",
    logo_opacity: float = 0.30, logo_scale: float = 0.12,
    log_cb=None, progress_cb=None, compress_after: bool = False,
) -> str:
    """Always burn first at quality 16; optionally encode a separate smaller delivery.
    A good burn is never removed until the compressed delivery passes QC.
    """
    if not ass_path or not Path(ass_path).is_file():
        raise ValueError("ملف الترجمة مطلوب لمرحلة الحرق الأساسية.")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = str(time.time_ns())
    burn = out.with_name(out.stem + ".burned." + token + ".mp4")
    burn_tmp = out.with_name(out.stem + ".burning." + token + ".mp4")
    compressed = out.with_name(out.stem + ".compressing." + token + ".mp4")
    def log(message):
        if log_cb:
            log_cb(message)
    def progress(stage, start, scale):
        def emit(pct, metric, detail):
            if progress_cb:
                progress_cb(start + pct * scale, stage + " · " + metric, detail)
        return emit
    free = shutil.disk_usage(out.parent).free
    if free < 256 * 1024 * 1024:
        raise OSError("المساحة أقل من 256MB. فضّي مساحة قبل الحرق.")
    if free < Path(video_path).stat().st_size * (6 if compress_after else 4):
        log("⚠️ مساحة العمل قليلة بالنسبة لحجم المصدر. الحرق عالي الجودة ممكن يكبر، والضغط يحتاج وجود الملفين معًا؛ التقدير مش ضمان.")
    log("🔥 BURN · حرق الترجمة واللوجو أولًا، جودة 16. مفيش تصغير حجم مستهدف في المرحلة دي.")
    try:
        burn_and_compress_native(video_path, ass_path, str(burn_tmp), fonts_dir,
            codec=codec, cq=16, preset=preset, x265_preset=x265_preset,
            audio_bitrate="copy", logo_path=logo_path, logo_position=logo_position,
            logo_opacity=logo_opacity, logo_scale=logo_scale, log_cb=log_cb,
            progress_cb=progress("BURN · الحرق", 0, 0.5 if compress_after else 0.95))
        report = qc_burned_episode(video_path, str(burn_tmp), log_cb=log_cb)
        if not report.get("ok"):
            raise RuntimeError("فحص الملف المحروق فشل؛ الملف المؤقت محفوظ: " + str(burn_tmp))
        os.replace(burn_tmp, burn)
        if not compress_after:
            log("⏭️ SKIP COMPRESSION · الضغط متوقف للحلقة. الملف المحروق هو الناتج النهائي.")
            os.replace(burn, out)
        else:
            log("📦 COMPRESS · ضغط منفصل للملف المحروق، بدون حرق الترجمة أو اللوجو مرة تانية.")
            if shutil.disk_usage(out.parent).free < max(burn.stat().st_size, 256 * 1024 * 1024):
                raise OSError("مساحة الضغط غير كافية. الملف المحروق محفوظ: " + str(burn))
            burn_and_compress_native(str(burn), "", str(compressed), fonts_dir,
                codec=codec, cq=int(cq), preset=preset, x265_preset=x265_preset,
                audio_bitrate=audio_bitrate, logo_path="", log_cb=log_cb,
                progress_cb=progress("COMPRESS · الضغط", 50, 0.45))
            report = qc_burned_episode(str(burn), str(compressed), log_cb=log_cb)
            if not report.get("ok"):
                raise RuntimeError("فحص النسخة المضغوطة فشل.")
            os.replace(compressed, out)
            burn.unlink()
        if progress_cb:
            progress_cb(100, "اكتمل الحرق" + (" والضغط المنفصل" if compress_after else " بدون ضغط إضافي"), out.name)
        return str(out)
    except BaseException as exc:
        if burn.exists():
            log("⚠️ الملف المحروق السليم محفوظ للاسترجاع: " + str(burn))
        # Never promote a failed/partial compression to the final output.
        if compressed.exists():
            compressed.unlink()
        raise


def make_browser_preview(
    video_path: str,
    output_path: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """Fast browser-playable copy (H.264/AAC mp4) of a finished episode for the
    Gradio preview player, so the player never triggers Gradio's own slow CPU
    re-encode of the HEVC master. Tries h264_nvenc (GPU) first and falls back to
    libx264 veryfast on CPU. Returns output_path; raises only if both attempts fail."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    _, _, _, total_dur = probe_video_info(video_path)

    attempts: List[Tuple[str, List[str]]] = []
    if _has_nvenc("h264_nvenc"):
        attempts.append(("h264_nvenc", [
            "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "28", "-b:v", "0",
            "-pix_fmt", "yuv420p",
        ]))
    attempts.append(("libx264", [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
    ]))

    PROGRESS_KEYS = (
        "frame=", "fps=", "stream_", "bitrate=", "total_size=",
        "out_time", "dup_frames=", "drop_frames=", "speed=", "progress=",
    )
    last_err = ""
    for engine, v_args in attempts:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-map", "0:v:0", "-map", "0:a:0?",
            *v_args,
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            output_path,
        ]
        if log_cb:
            log_cb(f"[preview] تجهيز نسخة معاينة سريعة للمتصفح بمحرك {engine}...")
        if progress_cb:
            progress_cb(0.5, f"تشغيل محرك {engine} للمعاينة...", Path(output_path).name)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        tail: List[str] = []
        pstate: Dict[str, str] = {}
        last_log_t = 0.0
        for line in proc.stdout:
            clean = line.strip()
            if not clean:
                continue
            if clean.startswith(PROGRESS_KEYS) and "=" in clean:
                k, v = clean.split("=", 1)
                pstate[k.strip()] = v.strip()
                if k.strip() == "progress":
                    cur_sec = _parse_hms(pstate.get("out_time", "00:00:00"))
                    spd_v = pstate.get("speed", "—")
                    pct = min(99.8, (cur_sec / total_dur) * 100.0) if total_dur > 0 else 50.0
                    if progress_cb:
                        progress_cb(pct, f"معاينة {engine} · السرعة: {spd_v}", Path(output_path).name)
                    now_t = time.monotonic()
                    if log_cb and (now_t - last_log_t >= 8.0):
                        last_log_t = now_t
                        log_cb(f"[preview] {pct:.1f}% | السرعة: {spd_v}")
            else:
                tail.append(clean)
                if len(tail) > 25:
                    tail.pop(0)
        if proc.wait() == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            src_mb = os.path.getsize(video_path) / (1024 * 1024)
            pv_mb = os.path.getsize(output_path) / (1024 * 1024)
            if progress_cb:
                progress_cb(100.0, "نسخة المعاينة جاهزة ✓", f"{pv_mb:.0f} MB · {engine}")
            if log_cb:
                log_cb(f"[preview] نسخة المعاينة جاهزة: {src_mb:.1f} MB -> {pv_mb:.1f} MB ({engine})")
            return output_path
        last_err = "\n".join(tail[-8:])
        if log_cb:
            log_cb(f"[preview] محرك {engine} لم ينجح — تجربة بديل أبطأ...")
    raise RuntimeError("تعذر تجهيز نسخة المعاينة:\n" + last_err)


def upload_to_rclone(
    local_file: str,
    remote_name: str,
    folder_path: str = "Runpod_Anime",
    generate_link: bool = True,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    fname = os.path.basename(local_file)
    dest_dir = f"{remote_name.strip().rstrip(':')}:{folder_path.strip().strip('/')}"
    dest_file = f"{dest_dir}/{fname}"
    if progress_cb:
        progress_cb(1.0, f"بدء الرفع إلى {remote_name}...", fname)

    proc = subprocess.Popen(
        ["rclone", "copy", local_file, dest_dir, "--stats=1s", "--stats-one-line", "-v"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    tail: List[str] = []
    last_log_t = 0.0
    for line in proc.stdout:
        clean = line.strip()
        if not clean:
            continue
        tail.append(clean)
        if len(tail) > 15:
            tail.pop(0)
        pct_m = re.search(r"(\d+(?:\.\d+)?)%", clean)
        if pct_m:
            pct = float(pct_m.group(1))
            spd_m = re.search(r"([\d.]+\s*\w+B/s)", clean)
            eta_m = re.search(r"ETA\s*([^\s,]+)", clean)
            spd = spd_m.group(1) if spd_m else "—"
            eta = eta_m.group(1) if eta_m else "—"
            if progress_cb:
                progress_cb(pct, f"السرعة: {spd} · المتبقي: {eta}", fname)
            now_t = time.monotonic()
            if log_cb and (now_t - last_log_t >= 6.0):
                last_log_t = now_t
                log_cb(f"[rclone:{remote_name}] {pct:.0f}% | {spd} | ETA {eta}")

    if proc.wait() != 0:
        raise RuntimeError(f"Rclone upload to {remote_name} failed:\n" + "\n".join(tail[-6:]))

    if progress_cb:
        progress_cb(100.0, f"اكتمل الرفع على {remote_name} ✓", fname)

    if generate_link:
        link_res = subprocess.run(["rclone", "link", dest_file], capture_output=True, text=True)
        if link_res.returncode == 0 and link_res.stdout.strip():
            return link_res.stdout.strip()
    return dest_file


def upload_to_doodstream(
    local_file: str,
    api_key: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Dict[str, str]:
    key = api_key.strip()
    if not key:
        raise ValueError("يرجى إدخال مفتاح DoodStream API Key.")

    req = urllib.request.Request(f"https://doodapi.co/api/upload/server?key={key}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        server_data = json.loads(resp.read().decode("utf-8"))
    if server_data.get("status") != 200 or not server_data.get("result"):
        raise RuntimeError(f"DoodStream server error: {server_data}")

    upload_url = f"{server_data['result']}?{key}"
    fname = os.path.basename(local_file)
    if log_cb:
        log_cb(f"[DoodStream] جاري رفع {fname}...")
    if progress_cb:
        progress_cb(1.0, "جاري الرفع على سيرفر DoodStream...", fname)

    proc = subprocess.Popen(
        ["curl", "--no-buffer", "-#", "-X", "POST", "-F", f"api_key={key}", "-F", f"file=@{local_file}", upload_url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out_chunks: List[bytes] = []

    def _collect_stdout() -> None:
        assert proc.stdout is not None
        while True:
            b = proc.stdout.read(4096)
            if not b:
                break
            out_chunks.append(b)

    t_out = threading.Thread(target=_collect_stdout, daemon=True)
    t_out.start()

    assert proc.stderr is not None
    buf = ""
    last_pct = -1.0
    while True:
        chunk = proc.stderr.read(128)
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="ignore")
        parts = re.split(r"[\r\n]+", buf)
        buf = parts[-1]
        for seg in parts[:-1]:
            m = re.search(r"(\d+(?:\.\d+)?)%", seg)
            if m:
                pct = float(m.group(1))
                if pct - last_pct >= 0.5 or pct >= 100.0:
                    last_pct = pct
                    if progress_cb:
                        progress_cb(pct, "رفع مباشر إلى DoodStream", fname)

    t_out.join()
    if proc.wait() != 0:
        raise RuntimeError(f"DoodStream curl error: {buf[-300:]}")

    raw_json = b"".join(out_chunks).decode("utf-8", errors="replace")
    data = json.loads(raw_json or "{}")
    if data.get("status") != 200 or not data.get("result"):
        raise RuntimeError(f"DoodStream API error: {data}")

    if progress_cb:
        progress_cb(100.0, "اكتمل الرفع على DoodStream ✓", fname)

    item = data["result"][0] if isinstance(data["result"], list) else data["result"]
    code = item.get("filecode", "")
    return {
        "filecode": code,
        "watch_url": item.get("download_url") or f"https://doodstream.com/d/{code}",
        "embed_url": item.get("protected_embed") or f"https://doodstream.com/e/{code}",
    }



def rclone_remote_configured(remote_name: str) -> bool:
    """True when `rclone listremotes` shows the named remote (e.g. gdrive:)."""
    try:
        res = subprocess.run(
            ["rclone", "listremotes"], capture_output=True, text=True, timeout=20
        )
        want = remote_name.strip().rstrip(":") + ":"
        return want in res.stdout.split()
    except Exception:
        return False


def drive_download_file(
    remote_name: str,
    folder_path: str,
    fname: str,
    local_path: str,
    log_cb: Optional[Callable[[str], None]] = None,
) -> bool:
    """Best-effort restore of one file from Drive. Never raises; False when absent.

    Uses --ignore-existing so a newer local file (same pod) is never overwritten.
    """
    src = f"{remote_name.strip().rstrip(':')}:{folder_path.strip().strip('/')}/{fname}"
    try:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            ["rclone", "copyto", src, local_path, "--ignore-existing"],
            capture_output=True, text=True, timeout=300,
        )
        ok = res.returncode == 0 and Path(local_path).is_file()
        if ok and log_cb:
            log_cb(f"☁️ تم استرجاع {fname} من Google Drive.")
        return ok
    except Exception as exc:
        if log_cb:
            log_cb(f"⚠️ تعذر استرجاع {fname} من Drive ({exc})")
        return False

def qc_burned_episode(
    source_path: str,
    output_path: str,
    ass_path: str = "",
    expected_lines: int = 0,
    qc_dir: str = "",
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Dict[str, object]:
    """Post-burn QC: output exists and is sizable, duration matches source,
    3 sample frames extracted for review, ASS dialogue count vs translation count."""
    def log(m: str) -> None:
        if log_cb:
            log_cb(m)

    def prog(pct: float, stage: str, detail: str = "") -> None:
        if progress_cb:
            try:
                progress_cb(pct, stage, detail)
            except Exception:
                pass

    report: Dict[str, object] = {"ok": True, "issues": []}

    def issue(msg: str) -> None:
        report["ok"] = False
        report["issues"].append(msg)  # type: ignore[attr-defined]
        log(f"⚠️ QC: {msg}")

    out = Path(output_path)
    name = out.name
    prog(8.0, "فحص الملف الناتج...", name)
    if not out.is_file() or out.stat().st_size < 256 * 1024:
        issue(f"الملف الناتج مفقود أو صغير بشكل غير طبيعي: {name}")

    size_mb = out.stat().st_size / (1024 * 1024) if out.is_file() else 0.0
    prog(30.0, "مطابقة المدة مع الأصل...", name)
    src_dur = out_dur = 0.0
    try:
        _, _, _, src_dur = probe_video_info(source_path)
        _, _, _, out_dur = probe_video_info(output_path)
        report["src_dur"] = src_dur
        report["out_dur"] = out_dur
        if src_dur > 0 and out_dur > 0 and abs(src_dur - out_dur) > max(2.0, src_dur * 0.01):
            issue(f"مدة الناتج ({out_dur:.1f}s) مختلفة عن الأصل ({src_dur:.1f}s)")
    except Exception as exc:
        log(f"⚠️ QC: تعذر فحص المدة ({exc})")

    prog(55.0, "استخراج لقطات المعاينة...", name)
    frames_ok = 0
    if qc_dir and out_dur > 0:
        try:
            Path(qc_dir).mkdir(parents=True, exist_ok=True)
            for i, frac in enumerate((0.1, 0.5, 0.9), 1):
                jpg = Path(qc_dir) / f"{out.stem}_qc{i}.jpg"
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{out_dur * frac:.1f}",
                     "-i", str(out), "-frames:v", "1", "-q:v", "4", str(jpg)],
                    capture_output=True, timeout=90,
                )
                if jpg.is_file() and jpg.stat().st_size > 4096:
                    frames_ok += 1
                prog(55.0 + i * 10.0, f"لقطة معاينة {i}/3...", name)
            if frames_ok < 3:
                issue(f"تم استخراج {frames_ok}/3 لقطات معاينة فقط")
        except Exception as exc:
            log(f"⚠️ QC: تعذر استخراج لقطات المعاينة ({exc})")

    prog(88.0, "عد أسطر الحوار...", name)
    n_dlg = 0
    if ass_path and os.path.isfile(ass_path) and expected_lines:
        try:
            with open(ass_path, encoding="utf-8", errors="ignore") as fh:
                n_dlg = sum(1 for ln in fh if ln.startswith("Dialogue:"))
            report["ass_lines"] = n_dlg
            report["expected_lines"] = expected_lines
            # أحداث Dialogue أكثر من الترجمات أمر طبيعي مع التقسيم التتابعي للأسطر الطويلة؛
            # العيب الحقيقي هو أسطر ناقصة فقط
            if n_dlg < expected_lines - max(2, int(expected_lines * 0.02)):
                issue(f"أسطر الحوار في ASS ({n_dlg}) أقل من الأسطر المترجمة ({expected_lines}) — في أسطر ناقصة")
        except Exception as exc:
            log(f"⚠️ QC: تعذر عد أسطر ASS ({exc})")

    report["size_mb"] = round(size_mb, 1)
    report["frames"] = frames_ok
    if report["ok"]:
        log(
            f"✅ QC سليم لـ {name}: المدة {out_dur:.0f}s"
            + (f" (الأصل {src_dur:.0f}s)" if src_dur else "")
            + f" · الحجم {size_mb:.0f}MB"
            + (f" · الأسطر {n_dlg}/{expected_lines}" if expected_lines else "")
            + (f" · {frames_ok} لقطات معاينة" if frames_ok else "")
        )
    else:
        log(f"⚠️ QC لـ {name}: {len(report['issues'])} ملاحظة — راجع السجل")
    prog(
        100.0,
        "اكتمل الفحص ✓" if report["ok"] else "اكتمل الفحص مع ملاحظات ⚠️",
        f"{size_mb:.0f}MB · {name}",
    )
    return report



# ═══ أرشفة الحلقات (RAR/7z) ═══

def find_archive_tool() -> Optional[str]:
    """rar لو موجود (أرشيف rar حقيقي)، وإلا 7z (WinRAR بيفتحه برضه)."""
    return shutil.which("rar") or shutil.which("7z") or shutil.which("7za")


def make_rar_archive(
    files: List[str],
    out_path: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[float, str, str], None]] = None,
) -> str:
    """يجمع ملفات في أرشيف واحد بوضع التخزين (بدون إعادة ضغط = سرعة النسخ).
    بيرجع مسار الأرشيف الفعلي (.rar مع rar أو .7z مع 7z)."""
    def log(m: str) -> None:
        if callable(log_cb):
            try:
                log_cb(m)
            except Exception:
                pass

    def prog(p: float, met: str, det: str = "") -> None:
        if callable(progress_cb):
            try:
                progress_cb(p, met, det)
            except Exception:
                pass

    tool = find_archive_tool()
    if not tool:
        raise RuntimeError("مفيش أداة أرشفة على البود — نفّذ: apt-get install -y rar p7zip-full")
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise RuntimeError("مفيش ملفات للأرشفة.")

    out = Path(out_path)
    is_7z = os.path.basename(tool).startswith("7z")
    if is_7z:
        out = out.with_suffix(".7z")
    total = len(files)
    total_bytes = sum(os.path.getsize(f) for f in files)
    work_dir = os.path.dirname(os.path.abspath(files[0]))
    names = [os.path.basename(f) for f in files]

    if is_7z:
        cmd = [tool, "a", "-mx=0", "-y", str(out)] + names
    else:
        cmd = [tool, "a", "-m0", "-o+", str(out)] + names

    log(f"📦 بدء الأرشفة ({os.path.basename(tool)}) · {total} ملف · {total_bytes / 1048576:.0f} ميجا ← {out.name}")
    prog(1.0, f"0/{total} ملف", "أرشفة بدون إعادة ضغط")
    proc = subprocess.Popen(
        cmd, cwd=work_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    )
    done = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        matched = next((n for n in names if n in line), None)
        if matched:
            done += 1
            log(f"   ➕ {matched}")
            prog(min(99.0, done * 100.0 / total), f"{done}/{total} ملف", "أرشفة بدون إعادة ضغط")
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"أداة الأرشفة فشلت (كود {rc}) — راجع السجل.")
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError("الأرشيف ما اتعملش — راجع السجل.")
    size_mb = out.stat().st_size / 1048576
    log(f"✅ الأرشيف جاهز: {out.name} ({size_mb:.0f} ميجا، {total} ملف)")
    prog(100.0, "اكتمل الأرشيف ✓", f"{out.name} · {size_mb:.0f}MB")
    return str(out)


# ═══ تسمية الإخراج ببراند الأداة ═══

BRAND_TAG = "[Animerza]"

_NAME_JUNK_RE = re.compile(
    r"(?i)\b(1080p|720p|480p|2160p|4k|x264|x265|h\.?264|h\.?265|hevc|av1|avc|aac[0-9.]*|flac|opus|mp3|"
    r"10bit|8bit|web[- ]?dl|webrip|bdrip|blu[- ]?ray|hdtv|dvd|dual[- ]?audio|multi[- ]?subs?|"
    r"multiple subtitle|proper|repack|subbed|dubbed|raw|batch|ddp[0-9.]*|dd\+|dts|truehd|atmos|5\.1|2\.0)\b"
)
_SE_TOKEN_RE = re.compile(r"(?i)S(\d{1,2})\s*E(\d{1,4})")
_SEASON_WORD_RE = re.compile(r"(?i)\bseason\s*(\d{1,2})\b")
_EPISODE_WORD_RE = re.compile(r"(?i)\b(?:episode|ep)\s*(\d{1,4})\b")


def magnet_display_name(link: str) -> str:
    """اسم العرض dn= من رابط ماجنت (فوري، بدون شبكة)."""
    m = re.search(r"[?&]dn=([^&]+)", link or "")
    if not m:
        return ""
    try:
        import urllib.parse as _up
        return _up.unquote_plus(m.group(1)).strip()
    except Exception:
        return m.group(1).strip()


def dotted_core(name: str) -> str:
    """يفضّل الفواصل بنقط: 'Black Clover S02E09' ← 'Black.Clover.S02E09'."""
    s = re.sub(r"[\s_]+", ".", (name or "").strip())
    s = re.sub(r"[^\w.\-\[\]]+", "", s)
    s = re.sub(r"\.{2,}", ".", s).strip(".")
    return s


def branded_stem(core: str, quality: str = "") -> str:
    """الاسم النهائي بدون امتداد: Core.1080p.[Animerza]"""
    c = dotted_core(core)
    if quality:
        c = f"{c}.{quality}"
    return f"{c}.{BRAND_TAG}"


def quality_token(height: int) -> str:
    if not height:
        return ""
    if height >= 2000:
        return "2160p"
    if height >= 1000:
        return "1080p"
    if height >= 700:
        return "720p"
    if height >= 400:
        return "480p"
    return f"{int(height)}p"


def clean_torrent_title(raw: str) -> Dict[str, Any]:
    """ينضّف اسم تورنت/ملف فانساب ← {series, season, episode, core} بنقط.
    مثال: '[SubsPlease] Black Clover S02 - 09 (1080p) [ABC123].mkv' ← Black.Clover.S02E09"""
    stem = Path((raw or "").strip()).stem
    s = re.sub(r"\[[^\]]*\]", " ", stem)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.replace("_", " ")
    season: Optional[int] = None
    episode: Optional[int] = None
    m = _SE_TOKEN_RE.search(s)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        series_part = s[: m.start()]
    else:
        ms, me = _SEASON_WORD_RE.search(s), _EPISODE_WORD_RE.search(s)
        series_part = s
        if ms and me:
            season, episode = int(ms.group(1)), int(me.group(1))
            series_part = s[: min(ms.start(), me.start())]
        elif me:
            episode = int(me.group(1))
            series_part = s[: me.start()]
        elif ms:
            season = int(ms.group(1))
            series_part = s[: ms.start()]
        else:
            mdash = re.search(r"(?i)\bS(\d{1,2})\s*[-–—]\s*(\d{1,4})\b", s)
            if mdash and int(mdash.group(2)) not in _RESOLUTION_NUMBERS:
                season, episode = int(mdash.group(1)), int(mdash.group(2))
                series_part = s[: mdash.start()]
            else:
                mnum = re.search(r"[-–—]\s*(\d{1,4})\s*$", s)
                if mnum and int(mnum.group(1)) not in _RESOLUTION_NUMBERS:
                    episode = int(mnum.group(1))
                    series_part = s[: mnum.start()]
                else:
                    epn = extract_episode_number(stem)
                    if epn is not None:
                        episode = epn
                        mtok = re.search(rf"(?<!\d){epn}(?!\d)", s)
                        series_part = s[: mtok.start()] if mtok else s
    series = _NAME_JUNK_RE.sub(" ", series_part)
    series = re.sub(r"[\-–—]+", " ", series)
    series = re.sub(r"\s+", " ", series).strip(" .-")
    core = dotted_core(series)
    if season is not None and episode is not None:
        tail = f"S{season:02d}E{episode:02d}"
    elif episode is not None:
        tail = f"EP{episode:02d}"
    elif season is not None:
        tail = f"S{season:02d}"
    else:
        tail = ""
    if tail:
        core = f"{core}.{tail}" if core else tail
    if not core:
        core = dotted_core(stem)
    return {"series": series, "season": season, "episode": episode, "core": core}


def torrent_episode_name(
    source_link: Optional[str] = None,
    torrent_file: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """اسم حلقة نضيف (بنقط) من ماجنت/.torrent — '' لو مش متاح."""
    link = (source_link or "").strip()
    if link.lower().startswith("magnet:"):
        dn = magnet_display_name(link)
        if dn:
            return clean_torrent_title(dn)["core"]
    try:
        files = list_torrent_files(source_link, torrent_file, log_cb=log_cb)
    except Exception:
        return ""
    vids = [f for f in files if Path(f[1]).suffix.lower() in VIDEO_EXTENSIONS]
    if not vids:
        return ""
    biggest = max(vids, key=lambda f: f[2])
    return clean_torrent_title(Path(biggest[1]).name)["core"]
