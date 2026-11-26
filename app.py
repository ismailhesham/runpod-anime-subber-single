import hashlib
import html
import inspect
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import gradio as gr
from ippo_theme import THEME_HEAD, THEME_CSS, HEADER_HTML

from media_ops import (
    VIDEO_EXTENSIONS,
    _has_nvenc,
    burn_and_compress_native,
    burn_episode,
    qc_burned_episode,
    collect_episode_videos,
    download_season_batch,
    download_torrent_or_url,
    episode_sort_key,
    list_torrent_files,
    extract_episode_number,
    make_browser_preview,
    make_rar_archive,
    extract_card_frame,
    probe_video_info,
    branded_stem,
    clean_torrent_title,
    dotted_core,
    magnet_display_name,
    quality_token,
    torrent_episode_name,
    BRAND_TAG,
    rclone_remote_configured,
    drive_download_file,
    upload_to_doodstream,
    upload_to_rclone,
)
from subtitles_ai import (
    _glossary_dir,
    _glossary_slug,
    ensure_style_profile,
    extract_embedded_subtitle,
    verify_full_dialogue_sub,
    learn_glossary_from_translation,
    load_or_extract_series_glossary,
    parse_source_subtitles,
    probe_embedded_subtitles,
    save_series_glossary,
    transcribe_with_whisper,
    translate_entries,
    write_responsive_ass,
)

# رفع تيليجرام اختياري: لو الملف أو مكتبة telethon مش موجودين، الأداة تفضل شغالة عادي
try:
    import telegram_channel as tgchan
except Exception:
    tgchan = None

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"
LOGO_FILE = BASE_DIR / "assets" / "logo.png"
STORAGE_ROOT = Path(os.environ.get("RUNPOD_OUTPUT_ROOT", "/workspace/outputs"))
RAW_CACHE_ROOT = Path(os.environ.get("RUNPOD_RAW_CACHE", "/workspace/raw_episodes"))
LEGACY_TEMP_ROOT = Path(os.environ.get("RUNPOD_TEMP_ROOT", "/workspace/tmp_downloads"))
SETTINGS_FILE = Path(os.environ.get("RUNPOD_SETTINGS_FILE", "/workspace/subber_settings.json"))

# المفاتيح (DeepSeek / DoodStream / Drive) مش بتتخزن في الريبو خالص.
# بتتقري بالترتيب: متغيرات البيئة ← /workspace/subber_secrets.json ← subber_secrets.json جنب الكود (متجاهَل في git).
_SECRETS_FILE = Path(os.environ.get("RUNPOD_SECRETS_FILE", "/workspace/subber_secrets.json"))
_LOCAL_SECRETS_FILE = BASE_DIR / "subber_secrets.json"
_SECRET_NAMES = ("DEEPSEEK_API_KEY", "DOODSTREAM_API_KEY", "GDRIVE_TOKEN", "RCLONE_CONF")


def _load_secrets_file(path: Path) -> None:
    try:
        if path.is_file():
            for _k, _v in json.loads(path.read_text(encoding="utf-8")).items():
                if isinstance(_v, str) and _v.strip():
                    os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass


for _sf in (_SECRETS_FILE, _LOCAL_SECRETS_FILE):
    _load_secrets_file(_sf)


def _persist_secrets(values: Dict[str, str]) -> None:
    """Save keys to /workspace/subber_secrets.json (chmod 600, outside git) and back them up to Drive."""
    try:
        cur: Dict[str, str] = {}
        if _SECRETS_FILE.is_file():
            try:
                cur = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
        for k, v in values.items():
            v = (v or "").strip()
            if v:
                cur[k] = v
                os.environ[k] = v
        if os.environ.get("GDRIVE_TOKEN", "").strip():
            cur["GDRIVE_TOKEN"] = os.environ["GDRIVE_TOKEN"].strip()
        _SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRETS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(_SECRETS_FILE, 0o600)
        except Exception:
            pass
        _drive_backup_secrets()
    except Exception:
        pass


def _drive_secrets_target() -> str:
    _st = load_settings()
    _remote = str(_st.get("gdrive_remote", "gdrive")).strip() or "gdrive"
    _folder = str(_st.get("gdrive_folder", "Runpod_Anime")).strip() or "Runpod_Anime"
    return f"{_remote}:{_folder}/_state/subber_secrets.json", _remote


def _drive_backup_secrets() -> None:
    """Best effort: keep a private copy of the keys in the user's own Drive so a fresh pod can restore them."""
    try:
        if shutil.which("rclone") is None or not _SECRETS_FILE.is_file():
            return
        target, remote = _drive_secrets_target()
        if not rclone_remote_configured(remote):
            return
        # نسخة الدرايف من غير توكن الدرايف نفسه / كونفج rclone (مفيدين بس محلياً)
        import tempfile
        _cur = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
        _cur.pop("RCLONE_CONF", None)
        _cur.pop("GDRIVE_TOKEN", None)
        _fd, _tmp = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                json.dump(_cur, _f)
            os.chmod(_tmp, 0o600)
            subprocess.run(["rclone", "copyto", _tmp, target], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        finally:
            try:
                os.unlink(_tmp)
            except Exception:
                pass
    except Exception:
        pass


def _drive_restore_secrets() -> None:
    """Fresh pod: if no keys are present locally, pull them back from Drive (needs rclone gdrive linked)."""
    try:
        if _SECRETS_FILE.is_file() or shutil.which("rclone") is None:
            return
        target, remote = _drive_secrets_target()
        if not rclone_remote_configured(remote):
            return
        _SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rclone", "copyto", target, str(_SECRETS_FILE)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        if _SECRETS_FILE.is_file():
            try:
                os.chmod(_SECRETS_FILE, 0o600)
            except Exception:
                pass
            _load_secrets_file(_SECRETS_FILE)
    except Exception:
        pass


ensure_style_profile()


def _seed_rclone_from_secrets() -> None:
    """Fresh pod: rebuild ~/.config/rclone/rclone.conf for the gdrive remote from the
    saved Drive token (GDRIVE_TOKEN env or secrets file), so backups work without notebook cell 2.
    rclone refreshes the access token by itself via the refresh_token."""
    try:
        if shutil.which("rclone") is None:
            return
        conf = Path.home() / ".config" / "rclone" / "rclone.conf"
        _rc = os.environ.get("RCLONE_CONF", "").strip()
        if _rc and not conf.is_file():
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text(_rc + "\n", encoding="utf-8")
        tok = os.environ.get("GDRIVE_TOKEN", "").strip()
        if tok and not conf.is_file():
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text(
                f"[gdrive]\ntype = drive\nscope = drive\ntoken = {tok}\n",
                encoding="utf-8",
            )
    except Exception:
        pass



def apply_rclone_config(conf_text: str, api_key_val: str = "") -> str:
    """Paste-in rclone config: writes ~/.config/rclone/rclone.conf, keeps a copy in the secrets file, checks the remote."""
    try:
        if api_key_val and api_key_val.strip():
            _persist_secrets({"DEEPSEEK_API_KEY": api_key_val})
        txt = (conf_text or "").strip()
        if not txt:
            return "✅ اتحفظ مفتاح الترجمة." if (api_key_val or "").strip() else "الصق نص الكونفج الأول."
        if not re.search(r"^\s*\[[^\]]+\]", txt, re.M):
            return "❌ النص ده مش شكل كونفج rclone (لازم يبدأ بسطر زي [gdrive]). الصق الكتلة كلها."
        if shutil.which("rclone") is None:
            return "❌ rclone مش منزل على البود."
        conf = Path.home() / ".config" / "rclone" / "rclone.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(txt + "\n", encoding="utf-8")
        try:
            os.chmod(conf, 0o600)
        except Exception:
            pass
        _persist_secrets({"RCLONE_CONF": txt})
        res = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=20)
        names = [n.rstrip(":") for n in res.stdout.split() if n.strip()]
        if not names:
            return "❌ rclone مش شايف أي remote في النص ده."
        want = str(load_settings().get("gdrive_remote", "gdrive")).strip() or "gdrive"
        if want not in names:
            return f"⚠️ اتحفظ، بس مفيش remote اسمه `{want}`. الموجود: {', '.join(names)}. غيّر خانة Drive Remote لواحد منهم."
        t = subprocess.run(["rclone", "lsd", f"{want}:", "--max-depth", "1"], capture_output=True, text=True, timeout=45)
        if t.returncode != 0:
            return "❌ اتحفظ بس الدخول على Drive فشل: " + (t.stderr.strip().splitlines() or ["؟"])[-1][:200]
        _drive_backup_secrets()
        return f"✅ Drive اتربط (`{want}`)."
    except Exception as exc:
        return f"❌ {exc}"

_seed_rclone_from_secrets()


def _drive_restore_glossaries() -> None:
    """Fresh pod: pull back saved per-series glossaries from Drive (best effort)."""
    try:
        _st = load_settings()
        _drv_remote = str(_st.get("gdrive_remote", "gdrive")).strip() or "gdrive"
        if not rclone_remote_configured(_drv_remote):
            return
        _drv_folder = str(_st.get("gdrive_folder", "Runpod_Anime")).strip() or "Runpod_Anime"
        gdir = _glossary_dir()
        gdir.mkdir(parents=True, exist_ok=True)
        subprocess_res = os.system(
            f"rclone copy {_drv_remote}:{_drv_folder}/_state/glossaries "
            + str(gdir) + " --ignore-existing >/dev/null 2>&1"
        )
        _ = subprocess_res
    except Exception:
        pass


def _drive_backup_glossary(series_name: str) -> bool:
    """Persist the per-series glossary to Drive so character names survive a pod wipe.
    Best effort: never raises, never blocks the run."""
    try:
        _st = load_settings()
        drv_remote = str(_st.get("gdrive_remote", "gdrive")).strip() or "gdrive"
        if not rclone_remote_configured(drv_remote):
            return False
        drv_folder = str(_st.get("gdrive_folder", "Runpod_Anime")).strip() or "Runpod_Anime"
        gfile = _glossary_dir() / f"{_glossary_slug(series_name)}.json"
        if not gfile.is_file():
            return False
        upload_to_rclone(str(gfile), drv_remote, f"{drv_folder}/_state/glossaries", generate_link=False)
        return True
    except Exception:
        return False


SERIES_NEW_OPT = "➕ أنمي جديد — اكتب الاسم بنفسك"


def list_saved_series() -> List[Tuple[str, str]]:
    """(label, value) choices for every saved per-series glossary, newest first.
    Runs after the Drive restore at startup, so a fresh pod still lists his anime."""
    out: List[Tuple[str, str]] = []
    try:
        gdir = _glossary_dir()
        if gdir.is_dir():
            files = sorted(gdir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            for fp in files:
                name = ""
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    name = str(data.get("series", "")).strip()
                except Exception:
                    pass
                if not name:
                    name = fp.stem.replace("_", " ").strip().title()
                if name:
                    out.append((f"📚 {name}", name))
    except Exception:
        pass
    # Include the actual output library even when no glossary exists yet.
    known = {value.casefold() for _, value in out}
    if STORAGE_ROOT.is_dir():
        for folder in sorted(STORAGE_ROOT.iterdir(), key=lambda p: p.name.casefold()):
            if folder.is_dir() and not folder.name.startswith("_") and folder.name.casefold() not in known:
                out.append(("📁 " + folder.name, folder.name))
                known.add(folder.name.casefold())
    return out


def _default_series_pick() -> str:
    saved = list_saved_series()
    return saved[0][1] if saved else SERIES_NEW_OPT

def _rescue_legacy_tmp_downloads() -> None:
    RAW_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if not LEGACY_TEMP_ROOT.is_dir():
        return
    for vid in LEGACY_TEMP_ROOT.rglob("*"):
        if not vid.is_file() or vid.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if vid.stat().st_size < 1024 * 1024 or any(vid.parent.glob("*.aria2")):
            continue
        dest_dir = RAW_CACHE_ROOT / f"rescued_{vid.parent.name}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / vid.name
        if not dest_file.exists():
            try:
                shutil.move(str(vid), str(dest_file))
            except Exception:
                pass


_rescue_legacy_tmp_downloads()


def _grind_rank(overall: float) -> str:
    if overall >= 99.5:
        return "S+"
    if overall >= 80:
        return "S"
    if overall >= 60:
        return "A"
    if overall >= 40:
        return "B"
    if overall >= 20:
        return "C"
    return "D"


def render_progress_hud(
    pct: float = 0.0,
    badge: str = "HUNTER HUD · STANDBY",
    title: str = "جاهز — اختار الحلقات وحمّلها الأول",
    metric: str = "السرعة: — · المتبقي: —",
    detail: str = "الحلقات الخام والقواميس والموديلات محفوظة دائماً في /workspace",
    mode: str = "cyan",
    overall_pct: Optional[float] = None,
    grind_label: str = "",
) -> str:
    clamped = max(0.0, min(100.0, float(pct)))
    mode_cls = {"violet": "sb-mode-violet", "done": "sb-mode-done", "error": "sb-mode-error"}.get(mode, "")
    grind_html = ""
    if overall_pct is not None:
        ov = max(0.0, min(100.0, float(overall_pct)))
        rank = _grind_rank(ov)
        grind_html = f"""
        <div class="sb-grind">
            <div class="sb-grind-head">
                <span class="sb-rank-live" id="sb-rank-live">RANK {rank}</span>
                <span class="sb-grind-label">{html.escape(grind_label)}</span>
                <span class="sb-grind-sub">إجمالي الموسم {ov:.1f}%</span>
            </div>
            <div class="sb-prog-track">
                <div class="sb-prog-fill sb-fill-violet" style="width: {ov:.1f}%;"></div>
            </div>
        </div>
        """
    return f"""
    <div class="sb-prog-card {mode_cls}">
        <div class="sb-prog-top">
            <div>
                <div class="sb-prog-badge">{html.escape(badge)}</div>
                <div class="sb-prog-title">{html.escape(title)}</div>
            </div>
            <div class="sb-prog-pct">{clamped:.1f}%</div>
        </div>
        <div class="sb-prog-track">
            <div class="sb-prog-fill" style="width: {clamped:.1f}%;"></div>
        </div>
        <div class="sb-prog-meta">
            <span>⚡ {html.escape(metric)}</span>
            <span>📦 {html.escape(detail)}</span>
        </div>
        {grind_html}
    </div>
    """


def render_multi_hud(
    cards: List[Dict[str, Any]],
    queued: List[str],
    layout: str,
    done_n: int,
    fail_n: int,
    total: int,
    conc: int,
    warn: str = "",
) -> str:
    """لوحة تقدم متعددة الحلقات: كارت لكل حلقة شغالة (صورة + خطوة + شريط) بشكل شبكة أو صفوف.
    الشبكة = الشكل A، الصفوف = الشكل B."""
    ov = max(0.0, min(100.0, (done_n / max(1, total)) * 100.0))
    rank = _grind_rank(ov)
    warn_html = (
        f'<div style="margin-top:8px;font-size:12.5px;font-weight:700;color:var(--sb-amber);">⚠️ {html.escape(warn)}</div>'
        if warn else ""
    )
    head = f"""
    <div class="sb-prog-card sb-mode-violet" style="margin-bottom:12px;">
        <div class="sb-prog-top">
            <div>
                <div class="sb-prog-badge">MULTI GRIND · {conc}X</div>
                <div class="sb-prog-title">طحن الموسم: {done_n} من {total} حلقة خلصت{(' · ' + str(fail_n) + ' فشلت') if fail_n else ''}</div>
            </div>
            <div class="sb-prog-pct">{ov:.1f}%</div>
        </div>
        <div class="sb-prog-track"><div class="sb-prog-fill sb-fill-violet" style="width: {ov:.1f}%;"></div></div>
        <div class="sb-prog-meta"><span>🏆 RANK {rank}</span><span>⚡ {len(cards)} شغالة دلوقتي</span></div>
        {warn_html}
    </div>
    """

    def _card_html(c: Dict[str, Any], compact: bool = False) -> str:
        mode_cls = {"violet": "sb-mode-violet", "done": "sb-mode-done", "error": "sb-mode-error"}.get(c.get("mode", "cyan"), "")
        pct = max(0.0, min(100.0, float(c.get("pct", 0.0))))
        frame = c.get("frame") or ""
        if frame:
            thumb = f'<img src="data:image/jpeg;base64,{frame}" style="width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:5px;border:1px solid rgba(56,189,248,0.25);display:block;">'
        else:
            thumb = (
                '<div style="width:100%;aspect-ratio:16/9;border-radius:5px;border:1px dashed rgba(56,189,248,0.3);'
                'background:#060B18;display:flex;align-items:center;justify-content:center;'
                'color:var(--sb-muted);font-size:12px;">الصورة هتظهر أول ما الحرق يبدأ</div>'
            )
        return f"""
        <div class="sb-prog-card {mode_cls}" style="margin:0;">
            {thumb}
            <div class="sb-prog-top" style="margin-top:8px;margin-bottom:4px;">
                <div>
                    <div class="sb-prog-badge">BURN · {html.escape(str(c.get('ep','')))}</div>
                    <div class="sb-prog-title">{html.escape(str(c.get('name','')))}</div>
                </div>
                <div style="font-family:'JetBrains Mono',monospace;font-weight:900;color:var(--sb-cyan-bright);direction:ltr;font-size:19px;">{pct:.1f}%</div>
            </div>
            <div class="sb-prog-track"><div class="sb-prog-fill" style="width:{pct:.1f}%;"></div></div>
            <div class="sb-prog-meta" style="margin-top:5px;"><span>⚡ {html.escape(str(c.get('step','')))}</span></div>
        </div>
        """

    queue_html = ""
    if queued:
        queue_html = (
            '<div style="margin-top:10px;font-size:12px;color:var(--sb-muted);">⏳ في الطابور: '
            + " · ".join(html.escape(q) for q in queued) + "</div>"
        )

    cards_joined = "".join(_card_html(c) for c in cards)
    if layout == "rows":
        body = f'<div style="display:flex;flex-direction:column;gap:10px;">{cards_joined}</div>'
    else:
        body = f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;">{cards_joined}</div>'
    return head + body + queue_html


def load_settings() -> Dict:
    defaults = {
        "provider": "DeepSeek API (موصى به)",
        "llm_model": "deepseek-flash",
        "api_key": os.environ.get("DEEPSEEK_API_KEY", os.environ.get("GEMINI_API_KEY", "")),
        "base_url": "https://api.deepseek.com",
        "style_preset": "bleach_style",
        "wrap_mode": "sequential",
        "soften_exclaim": True,
        "dood_api_key": os.environ.get("DOODSTREAM_API_KEY", ""),
        "gdrive_remote": "gdrive",
        "gdrive_folder": "Runpod_Anime",
        "codec": "hevc_nvenc",
        "preset": "p7",
        "x265_preset": "slow",
        "cq": 27,
        "audio_bitrate": "copy",
        "concurrency": 3,
        "queue_layout": "grid",
        "credit_translator": "",
        "credit_typesetter": "",
        "credit_encoder": "",
        "logo_position": "top-right",
        "logo_opacity": 0.30,
        "logo_scale": 0.12,
    }
    if SETTINGS_FILE.is_file():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update({k: v for k, v in saved.items() if v is not None})
        except Exception:
            pass
    # ترحيل إجباري: ديب سيك فلاش بدل البرو (توفير استهلاك التوكنز)
    if defaults.get("llm_model") == "deepseek-v4-pro":
        defaults["llm_model"] = "deepseek-flash"
    return defaults


_drive_restore_secrets()
_drive_restore_glossaries()


def save_settings(
    provider, llm_model, api_key, base_url, style_preset, wrap_mode, soften_exclaim, dood_api_key,
    gdrive_remote, gdrive_folder,
    codec, preset, x265_preset, cq, audio_bitrate,
    concurrency, queue_layout,
    credit_translator, credit_typesetter, credit_encoder,
    logo_position, logo_opacity, logo_scale,
) -> str:
    data = {
        "provider": provider,
        "llm_model": llm_model,
        "api_key": None,  # المفاتيح مبتتكتبش في ملف الإعدادات، بتتحفظ في ملف الأسرار بس
        "base_url": base_url,
        "style_preset": style_preset,
        "wrap_mode": wrap_mode,
        "soften_exclaim": bool(soften_exclaim),
        "dood_api_key": None,
        "gdrive_remote": gdrive_remote,
        "gdrive_folder": gdrive_folder,
        "codec": codec,
        "preset": preset,
        "x265_preset": x265_preset,
        "cq": int(cq),
        "audio_bitrate": audio_bitrate,
        "concurrency": max(1, min(10, int(concurrency))),
        "queue_layout": "rows" if str(queue_layout) == "rows" else "grid",
        "credit_translator": credit_translator,
        "credit_typesetter": credit_typesetter,
        "credit_encoder": credit_encoder,
        "logo_position": logo_position,
        "logo_opacity": float(logo_opacity),
        "logo_scale": float(logo_scale),
    }
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except Exception:
        pass
    _persist_secrets({"DEEPSEEK_API_KEY": api_key, "DOODSTREAM_API_KEY": dood_api_key})
    return f"✅ تم حفظ جميع المفاتيح والإعدادات الدائمة في `{SETTINGS_FILE}`."


def step_save_manual_sub(series_name_val: str, ep_num, file_obj) -> str:
    """يحفظ ملف الترجمة المترجم يدوياً في المكان اللي وضع الموسم بيتراقبه.
    الحلقة اليدوية تلقطه وتكمل لوحدها — حتى لو الأداة شغالة دلوقتي."""
    if not file_obj:
        return "⚠️ اختر ملف الترجمة الأول."
    try:
        gi = int(ep_num)
    except Exception:
        return "⚠️ رقم الحلقة مش واضح."
    if gi < 1:
        return "⚠️ رقم الحلقة لازم يكون 1 أو أكتر."
    src_path = getattr(file_obj, "name", None) or str(file_obj)
    ext = Path(src_path).suffix.lower()
    if ext not in (".srt", ".ass"):
        return "⚠️ الملف لازم يكون SRT أو ASS."
    try:
        series_safe_m = _safe_name((series_name_val or "").strip() or "Anime_Season")
        d = RAW_CACHE_ROOT / "_manual_subs" / series_safe_m
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"EP{gi:03d}{ext}"
        shutil.copy2(src_path, str(dest))
        return f"✅ ملف الحلقة {gi} محفوظ ({dest.name}) — الأداة هتلقطه وتكمّل لوحدها لو شغالة."
    except Exception as exc:
        return f"❌ تعذر حفظ الملف: {exc}"


def _on_provider_change(prov: str) -> Tuple[Any, Any]:
    if "DeepSeek" in prov:
        return gr.update(value="deepseek-flash"), gr.update(value="https://api.deepseek.com")
    if "Gemini" in prov:
        return gr.update(value="gemini-3.8-flash"), gr.update(value="")
    return gr.update(value="deepseek-flash"), gr.update(value="http://localhost:8000/v1")


def _provider_key(prov_label: str) -> str:
    if "Gemini" in (prov_label or ""):
        return "gemini"
    if "DeepSeek" in (prov_label or ""):
        return "deepseek"
    return "openai"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", (name or "").strip())
    return cleaned or time.strftime("Episode_%Y%m%d_%H%M%S")


def _ms_fmt(ms: int) -> str:
    s_tot = max(0.0, int(ms) / 1000.0)
    m = int(s_tot // 60)
    s = s_tot - m * 60
    return f"{m:02d}:{s:05.2f}"


RUN_LOG_FILE = STORAGE_ROOT / "_logs" / "hunter_log.txt"


def _append_disk_log(line: str) -> None:
    try:
        RUN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RUN_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def list_local_outputs() -> List[str]:
    try:
        if not STORAGE_ROOT.exists():
            return []
        mp4s = sorted(STORAGE_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [str(p) for p in mp4s if not p.name.endswith("_preview.mp4")][:50]
    except OSError:
        return []


def list_cached_raw_videos() -> List[str]:
    try:
        _rescue_legacy_tmp_downloads()
        if not RAW_CACHE_ROOT.exists():
            return []
        vids = [
            p for p in RAW_CACHE_ROOT.rglob("*")
            if p.is_file()
            and p.suffix.lower() in VIDEO_EXTENSIONS
            and "_work" not in p.parts
            and "_player" not in p.parts
            and p.stat().st_size > 1024 * 1024
            and not any(p.parent.glob("*.aria2"))
        ]
        vids.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [str(p) for p in vids[:60]]
    except OSError:
        return []


def _raw_cache_dir_for_source(q_label: str, source_link: Optional[str], torrent_file: Optional[str], ep_name: str) -> Path:
    RAW_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if source_link and source_link.strip():
        s = source_link.strip()
        m_hash = re.search(r"btih:([a-zA-Z0-9]+)", s, re.IGNORECASE)
        key = f"magnet_{m_hash.group(1).lower()[:16]}" if m_hash else f"url_{hashlib.sha1(s.encode('utf-8')).hexdigest()[:14]}"
    elif torrent_file and os.path.isfile(torrent_file):
        h = hashlib.sha1(Path(torrent_file).read_bytes()).hexdigest()[:14]
        key = f"tor_{h}"
    else:
        key = _safe_name(ep_name)
    return RAW_CACHE_ROOT / f"{key}_{q_label}"


def _verify_wizard_video(path: Path, expected_size: int = 0) -> None:
    if not path.is_file() or Path(str(path) + ".aria2").exists():
        raise ValueError(f"الملف لسه ما اكتملش: {path.name}")
    if expected_size and path.stat().st_size != expected_size:
        raise ValueError(f"حجم الملف غير مكتمل: {path.name}")
    width, height, _, duration = probe_video_info(str(path))
    if not width or not height or not duration or duration <= 0:
        raise ValueError(f"ملف الفيديو غير قابل للقراءة: {path.name}")


def step_download_wizard(links_text, picks, file_map, series_name, series_pick, progress=gr.Progress()):
    """Download only. Never translate or burn."""
    logs = []
    def log(message):
        logs.append(str(message))
    yield [], {}, gr.update(interactive=False), "جاري التحميل...", gr.update(interactive=False)
    try:
        links = [line.strip() for line in (links_text or "").splitlines() if line.strip()]
        if not links:
            raise ValueError("حط رابط تورنت أو مسار فيديو الأول.")
        if not picks and any(not Path(link).exists() for link in links):
            raise ValueError("اعرض ملفات التورنت وعلّم على الحلقات المطلوبة الأول.")
        series = series_name.strip() if series_pick == SERIES_NEW_OPT else str(series_pick or series_name).strip()
        if not series:
            raise ValueError("اختار الأنمي أو اكتب اسمه الأول.")
        manifest = []
        for li, link in enumerate(links):
            selected = [int(file_map[value][1]) for value in (picks or [])
                        if value in (file_map or {}) and int(file_map[value][0]) == li]
            if Path(link).is_dir():
                files = collect_episode_videos(link)
                expected = [(path, 0, f"local:{path.resolve()}") for path in files]
            elif Path(link).is_file() and Path(link).suffix.lower() in VIDEO_EXTENSIONS:
                path = Path(link)
                expected = [(path, 0, f"local:{path.resolve()}")]
            else:
                if not selected:
                    continue
                listing = {int(i): (rel, size) for i, rel, size in list_torrent_files(link, None)}
                if any(i not in listing for i in selected):
                    raise ValueError("قائمة الملفات اتغيرت. اعرض التورنت واختار الحلقات من جديد.")
                root = _raw_cache_dir_for_source("season", link, None, f"{_safe_name(series)}_{li+1}")
                download_season_batch(link, None, str(root), log_cb=log,
                    progress_cb=lambda pct, metric, detail: progress(pct / 100, desc=str(metric)),
                    select_files=selected)
                source_key = hashlib.sha256(link.encode()).hexdigest()
                expected = [(root / listing[i][0], listing[i][1], f"torrent:{source_key}:{i}") for i in selected]
            for path, size, source_id in expected:
                _verify_wizard_video(path, size)
                resolved = str(path.resolve())
                if not any(item["path"] == resolved for item in manifest):
                    manifest.append({"id": source_id, "path": resolved, "name": path.name,
                                     "size": path.stat().st_size, "series": series})
        if not manifest:
            raise ValueError("مفيش حلقات مختارة أو ملفات فيديو جاهزة.")
        manifest.sort(key=lambda item: episode_sort_key(Path(item["path"])))
        for item in manifest:
            item["source_input"] = links_text
            item["picks"] = sorted(picks or [])
        yield manifest, {}, gr.update(interactive=True), f"✓ اتأكدنا من {len(manifest)} ملف فيديو. اضغط التالي لملفات الترجمة.", gr.update(interactive=True)
    except Exception as exc:
        yield [], {}, gr.update(interactive=False), "❌ " + str(exc) + "\n" + "\n".join(logs[-8:]), gr.update(interactive=True)


def _wizard_next(manifest, links=None, picks=None):
    if not manifest:
        raise gr.Error("حمّل الحلقات واتأكد منها الأول.")
    for item in manifest:
        if links is not None and (item.get("source_input") != links or item.get("picks") != sorted(picks or [])):
            raise gr.Error("الاختيار اتغير. حمّل الاختيار الحالي الأول.")
        _verify_wizard_video(Path(item["path"]), item["size"])
    return gr.update(visible=False), gr.update(visible=True)


def _wizard_subtitle_tracks(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced",
        "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("تعذرت قراءة مسارات الترجمة داخل الحلقة.")
    tracks = json.loads(result.stdout or "{}").get("streams", [])
    for pos, track in enumerate(tracks):
        track["position"] = pos
        track["supported"] = track.get("codec_name") in {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text"}
    return tracks


def _wizard_arabic_track(path):
    tracks = [t for t in _wizard_subtitle_tracks(path) if t["supported"] and t.get("tags", {}).get("language", "").lower() in {"ar", "ara", "arabic"}]
    # Prefer a full track over signs or forced-only tracks, then default.
    return max(tracks, key=lambda t: (not bool(t.get("disposition", {}).get("forced")), "sign" not in t.get("tags", {}).get("title", "").lower(), t.get("disposition", {}).get("default", 0))) if tracks else None


def _wizard_track_choices(tracks):
    choices = [("ارفع ملف SRT / ASS من جهازك", "upload")]
    for track in tracks:
        tags = track.get("tags", {})
        flags = track.get("disposition", {})
        label = f"#{track['index']} · {tags.get('language', 'und')} · {tags.get('title', 'بدون عنوان')} · {track.get('codec_name', '?')}"
        label += f" · default={flags.get('default', 0)} · forced={flags.get('forced', 0)}"
        if not track["supported"]:
            label += " · صورة: يحتاج OCR (غير متاح)"
        choices.append((label, str(track["index"])))
    return choices


def _wizard_export_track(path, track):
    out_dir = RAW_CACHE_ROOT / "_wizard_track_exports" / hashlib.sha256(str(path).encode()).hexdigest() / str(track["index"])
    out_dir.mkdir(parents=True, exist_ok=True)
    # Per-file, per-stream directory avoids overwriting another track.
    extension = ".ass" if track["codec_name"] in ("ass", "ssa") else ".srt"
    cached = out_dir / ("extracted_source" + extension)
    extracted = cached if cached.is_file() and cached.stat().st_size else Path(extract_embedded_subtitle(str(path), track["position"], track["codec_name"], str(out_dir)))
    destination = out_dir / (Path(path).stem + f".track-{track['index']}." + re.sub(r"[^a-zA-Z0-9_-]", "_", track.get("tags", {}).get("language", "und")) + extracted.suffix)
    shutil.copy2(extracted, destination)
    return str(destination)


def _wizard_choice(mode, upload, choices, item):
    updated = dict(choices or {})
    entry = dict(updated.get(item["path"], {}))
    entry["mode"] = mode
    if not upload:
        entry.pop("subtitle", None)
    if upload:
        source = Path(upload if isinstance(upload, str) else upload.name)
        if source.suffix.lower() not in (".srt", ".ass") or not parse_source_subtitles(str(source)):
            raise gr.Error("ارفع ملف SRT أو ASS صالح وفيه ترجمة.")
        dest_dir = RAW_CACHE_ROOT / "_wizard_subs" / hashlib.sha256(item["path"].encode()).hexdigest()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ("subtitle" + source.suffix.lower())
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        if entry.get("subtitle") != str(dest):
            entry.pop("source_kind", None)
        entry["subtitle"] = str(dest)
    updated[item["path"]] = entry
    return updated, gr.update(visible=mode == "ترجمة يدوية")


def step_process_episodes(
    source_links: str,
    series_name: str,
    series_pick: str,
    style_preset: str,
    wrap_mode: str,
    soften_exclaim: bool,
    sub_mode: str,
    whisper_model: str,
    provider: str,
    api_key: str,
    llm_model: str,
    base_url: str,
    codec: str,
    preset: str,
    x265_preset: str,
    audio_bitrate: str,
    credit_translator: str,
    credit_typesetter: str,
    credit_encoder: str,
    cleanup_temp: bool,
    logo_position: str,
    logo_opacity: float,
    logo_scale: float,
    enable_telegram: bool = False,
    concurrency: float = 3,
    queue_layout: str = "grid",
    manual_eps_text: str = "",
    downloaded_manifest=None,
    subtitle_choices=None,
):
    """Single-episode processing: translate + burn for the downloaded
    episode files, and collect the results. Resume-safe."""
    logs: List[str] = []
    grind_ctx = {"gi": 0, "total": 0}  # سياق شريط تقدم الموسم — كان غير معرّف في هذه الدالة (إصلاح عطل)

    def push(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        logs.append(line)
        _append_disk_log(line)
        print(line, flush=True)

    push(f"🗂️ نسخة دائمة من هذا السجل تُحفظ تلقائياً في: {RUN_LOG_FILE}")

    def pack(
        hud_html: str,
        preview_val=gr.update(),
        local_dd=gr.update(),
        raw_dd=gr.update(),
        vids_val=gr.update(),
        ass_val=gr.update(),
        state_val=gr.update(),
        g1=gr.update(),
        g2=gr.update(),
        dls=gr.update(),
        lnk=gr.update(),
        arch=gr.update(),
        arch_txt=gr.update(),
        subs=gr.update(),
    ):
        return (hud_html, "\n".join(logs[-150:]), preview_val, local_dd, raw_dd, vids_val, ass_val, state_val, g1, g2, dls, lnk, arch, arch_txt, subs)

    def run_live(fn: Callable, *args, badge: str, title: str, mode: str = "cyan", accepts_progress_cb: bool = True, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        if accepts_progress_cb:
            kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except BaseException as e:
                res_box["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        cur_pct, cur_met, cur_det = 0.0, "جاري البدء...", ""
        dirty = True
        last_yield = 0.0

        while t.is_alive() or not ev_q.empty():
            try:
                items = [ev_q.get(timeout=0.25)]
            except queue.Empty:
                items = []
            while not ev_q.empty():
                try:
                    items.append(ev_q.get_nowait())
                except queue.Empty:
                    break
            for k, p in items:
                dirty = True
                if k == "prog":
                    cur_pct, cur_met, cur_det = p  # type: ignore[misc]
                else:
                    msg_s = str(p)
                    if msg_s.startswith("[whisper_prog]"):
                        parts = msg_s.replace("[whisper_prog]", "").strip().split("|")
                        try:
                            cur_pct = float(parts[0].replace("%", "").strip())
                        except Exception:
                            pass
                        cur_met = "تفريغ صوتي بـ Faster-Whisper ⚡"
                        cur_det = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        push(msg_s)
                        bm = re.search(r"Translated batch\s+(\d+)/(\d+)", msg_s)
                        if bm:
                            bx, by = int(bm.group(1)), int(bm.group(2))
                            cur_pct = (bx / max(1, by)) * 100.0
                            cur_met = f"تمت ترجمة الدفعة {bx} من {by}"
                            cur_det = llm_model

            now_m = time.monotonic()
            if dirty and (now_m - last_yield >= 0.35 or not t.is_alive()):
                last_yield = now_m
                dirty = False
                yield pack(render_progress_hud(cur_pct, badge, title, cur_met, cur_det, mode=mode))

        if "err" in res_box:
            raise res_box["err"]  # type: ignore[misc]
        return res_box.get("val")

    if series_pick and series_pick != SERIES_NEW_OPT:
        series_name = series_pick
    links = [ln.strip() for ln in (source_links or "").splitlines() if ln.strip()]
    if not links:
        push("⚠️ أدخل رابط تورنت واحد على الأقل (رابط في كل سطر).")
        yield pack(render_progress_hud(0.0, "تنبيه · لا توجد روابط", "أدخل روابط التورنت أولاً", "—", "—", mode="error"), state_val={})
        return

    series_safe = _safe_name(series_name.strip() or "Anime_Season")
    series_dot = dotted_core(series_name.strip() or "Anime_Season") or "Anime"
    series_root = STORAGE_ROOT / series_safe
    ass_root = series_root / "_ass_files"
    try:
        series_root.mkdir(parents=True, exist_ok=True)
        ass_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        push(f"❌ تعذر تجهيز مجلدات الموسم على السيرفر: {exc}\n{traceback.format_exc()[-800:]}")
        yield pack(
            render_progress_hud(0.0, "ERROR · STORAGE", f"مشكلة في مساحة التخزين: {str(exc)[:110]}", "راجع مساحة /workspace على السيرفر", "—", mode="error"),
            state_val={},
        )
        return

    yield pack(
        render_progress_hud(0.0, "INIT · PREPARING", "تم تجهيز مساحة العمل — بدء طحن الموسم...", "—", "—"),
        g1=gr.update(visible=False),
        lnk=gr.update(visible=False),
        arch=gr.update(visible=False),
    )


    try:
        # ═══ المرحلة 1: تحميل كل الروابط وجمع كل الحلقات ═══
        all_eps: List[Path] = []
        if downloaded_manifest is not None:
            if not downloaded_manifest:
                raise ValueError("ارجع للخطوة الأولى وحمّل الحلقات.")
            for item in downloaded_manifest:
                _verify_wizard_video(Path(item["path"]), item["size"])
                if item["series"] != series_name:
                    # series_name below is the resolved anime identity.
                    if item["series"] != (series_pick if series_pick != SERIES_NEW_OPT else series_name):
                        raise ValueError("اسم الأنمي اتغير. ارجع لمرحلة التحميل.")
                choice = (subtitle_choices or {}).get(item["path"], {})
                if choice.get("mode") == "ترجمة يدوية":
                    sub = choice.get("subtitle", "")
                    if not sub or not Path(sub).is_file() or not parse_source_subtitles(sub):
                        raise ValueError(f"ارفع ترجمة {item['name']} قبل التشغيل.")
                all_eps.append(Path(item["path"]))
        unique = {str(p.resolve()): p for p in all_eps}
        all_eps = sorted(unique.values(), key=episode_sort_key)
        total = len(all_eps)
        if total == 0:
            raise FileNotFoundError("لم يتم العثور على أي حلقة في الروابط المدخلة.")

        grind_ctx["total"] = total
        push(f"📚 إجمالي الحلقات المكتشفة: {total} حلقة.")
        for pos, vp in enumerate(all_eps, 1):
            epn = extract_episode_number(vp.name)
            push(f"   {pos:03d}. {vp.name}" + (f"  (حلقة {epn})" if epn is not None else ""))

        # ═══ تسمية مخصصة من الواجهة: وضع الحلقة الواحدة برابطي 1080p و720p ═══
        # النسختين بياخدوا نفس ep_label (نفس الحلقة)، ومفتاح النتيجة بيميزهم بالجودة.
        _path_to_label: Dict[str, str] = {}
        _path_to_quality: Dict[str, str] = {}
        if downloaded_manifest is not None:
            for _it in downloaded_manifest:
                try:
                    _rp = str(Path(_it["path"]).resolve())
                except Exception:
                    continue
                if _it.get("ep_label"):
                    _path_to_label[_rp] = str(_it["ep_label"])
                if _it.get("quality"):
                    _path_to_quality[_rp] = str(_it["quality"])

        def _label_of(gi: int) -> str:
            try:
                return _path_to_label.get(str(all_eps[gi - 1].resolve()), f"EP{gi:03d}")
            except Exception:
                return f"EP{gi:03d}"

        def _label_shared(lbl: str) -> bool:
            return list(_path_to_label.values()).count(lbl) > 1

        def _render_key(gi: int, vh: int = 0) -> str:
            lbl = _label_of(gi)
            if not _label_shared(lbl):
                return lbl
            try:
                tok = _path_to_quality.get(str(all_eps[gi - 1].resolve()), "")
            except Exception:
                tok = ""
            if not tok and vh:
                tok = quality_token(vh)
            return f"{lbl} · {tok}" if tok else f"{lbl} · نسخة {gi}"

        def _work_name(gi: int) -> str:
            # فولدرات العمل والفحص لازم تفضل فريدة لما نسختين يشتركوا في نفس الرقم
            lbl = _label_of(gi)
            return f"{lbl}_{gi:03d}" if _label_shared(lbl) else lbl

        def _ep_num_of(gi: int, lbl: str) -> int:
            mnum = re.search(r"(\d+)", lbl or "")
            return int(mnum.group(1)) if mnum else gi

        # ═══ فولدر الموسم بالبراند: One.Piece.EP1-50.1080p.[Animerza] ═══
        _res_set = set()
        for _vp in all_eps:
            try:
                _, _vh, _, _ = probe_video_info(str(_vp))
                if _vh:
                    _res_set.add(quality_token(_vh))
            except Exception:
                pass
        _q_folder = f".{next(iter(_res_set))}" if len(_res_set) == 1 and next(iter(_res_set)) else ""
        branded_folder = f"{series_dot}.EP1-{total}{_q_folder}.{BRAND_TAG}"
        if downloaded_manifest is not None:
            identity = [{"flow": (downloaded_manifest[0].get("flow", "single")), "path": str(path.resolve()), "size": path.stat().st_size,
                         "subtitle": (subtitle_choices or {}).get(str(path.resolve()), {}),
                         "codec": codec, "burn_quality": 16} for path in all_eps]
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
            branded_folder += "." + digest
        new_root = STORAGE_ROOT / branded_folder
        if new_root != series_root:
            try:
                if new_root.is_dir():
                    series_root = new_root  # استكمال في فولدر موجود بنفس الاسم
                else:
                    if downloaded_manifest is None and series_root.is_dir() and any(series_root.iterdir()):
                        series_root.rename(new_root)
                    else:
                        new_root.mkdir(parents=True, exist_ok=True)
                    series_root = new_root
                ass_root = series_root / "_ass_files"
                ass_root.mkdir(parents=True, exist_ok=True)
                push(f"📁 فولدر الموسم: {series_root.name}")
            except Exception as exc:
                push(f"⚠️ تعذر تسمية فولدر الموسم بالبراند ({exc}) — هيكمل بالاسم الحالي.")

        # ═══ هيكل الإخراج: فولدر واحد، يتقسم لفولدرات 35 حلقة للأنمي الطويل ═══
        split_parts = total > 35

        def part_dir_for(global_i: int) -> Path:
            if not split_parts:
                return series_root
            part = (global_i - 1) // 35 + 1
            lo = (part - 1) * 35 + 1
            hi = min(part * 35, total)
            d = series_root / f"Part {part:02d} (EP{lo:03d}-{hi:03d})"
            d.mkdir(parents=True, exist_ok=True)
            return d

        if split_parts:
            n_parts = (total + 34) // 35
            push(f"🗂️ أنمي طويل ({total} حلقة): سيتم التقسيم إلى {n_parts} فولدرات × 35 حلقة.")

        credits_dict = {
            "translator": credit_translator,
            "typesetter": credit_typesetter,
            "encoder": credit_encoder,
        }
        has_credits = any(v.strip() for v in credits_dict.values())

        rendered: Dict[str, str] = {}
        failed: List[str] = []
        tg_warned: Dict[str, bool] = {"v": False}  # تحذير «تيليجرام مش جاهز» مرة واحدة بس في الموسم
        glossary_box: Dict[str, Any] = {"val": {}}  # صندوق مشترك: التجهيز المبكر بيحدثه في الخلفية
        last_ass: Optional[str] = None

        # ☁️ نسخ احتياطي على Google Drive: البود مؤقت وكل حاجة بتتمسح لما يتقفل
        _st = load_settings()
        drv_remote = str(_st.get("gdrive_remote", "gdrive")).strip() or "gdrive"
        drv_folder = str(_st.get("gdrive_folder", "Runpod_Anime")).strip() or "Runpod_Anime"
        # إصلاح drive_ok: لو الذاكرة اتنزلت من Drive أصلاً يبقى rclone شغال — متعطلش الرفع عند الاستكمال
        drive_ok = True if downloaded_manifest is not None else rclone_remote_configured(drv_remote)
        if drive_ok:
            push("☁️ Google Drive متربط — هيتم رفع كل حلقة أول بأول + حفظ الذاكرة والقاموس.")
            drive_download_file(drv_remote, f"{drv_folder}/_state/{series_safe}", "_translation_memory.json", str(series_root / "_translation_memory.json"))
            if drive_download_file(drv_remote, f"{drv_folder}/_state/glossaries", f"{_glossary_slug(series_name)}.json", str(_glossary_dir() / f"{_glossary_slug(series_name)}.json")):
                push("♻️ تم استرجاع قاموس الأنمي من Drive — الأسماء والمصطلحات زي ما وصلتله آخر مرة.")
        else:
            push("⚠️ Google Drive مش متربط (rclone) — الشغل هيتحفظ محلياً بس وهيتمسح لو البود اتقفل!")

        # ذاكرة ترجمة الموسم: الأسطر المكررة (الأوبنينج/الإندينج) تترجم مرة واحدة فقط
        tmem_path = series_root / "_translation_memory.json"
        tmem: Dict[str, str] = {}
        if tmem_path.is_file():
            try:
                tmem = {str(k): str(v) for k, v in json.loads(tmem_path.read_text(encoding="utf-8")).items()}
            except Exception:
                tmem = {}
        if tmem:
            push(f"♻️ ذاكرة ترجمة الموسم محملة: {len(tmem)} سطر جاهز لإعادة الاستخدام.")

        def _tkey(txt: str) -> str:
            return " ".join((txt or "").split())

        # ═══ المرحلة 2: معالجة كل حلقة مع تجهيز مبكر (Pipelining) ═══
        # الفكرة: أثناء حرق/ضغط/رفع الحلقة الحالية، خيط خلفية واحد يجهّز الحلقة
        # اللي بعدها (سحب ترجمة/تفريغ + ترجمة + ملف ASS). التجهيز نفسه ما بيشتغلش
        # أبداً في خيطين في نفس اللحظة، فمفيش تزاحم على ذاكرة الترجمة أو القاموس،
        # والحرق والرفع بيفضلوا بالترتيب زي ما هما بالظبط.

        # الحلقات الجاهزة مسبقاً تتخطى من الأول من غير أي إعادة تجهيز (استكمال بعد ريستارت)
        skip_set: set = set()
        for gi0 in range(1, total + 1):
            d0 = part_dir_for(gi0)
            lbl0 = _label_of(gi0)
            found0 = None
            if d0.is_dir():
                cands = sorted(d0.glob(f"{series_dot}.{lbl0}.*.mp4"))
                cands = [c for c in cands if not any(tag in c.stem for tag in ("_preview", ".burned.", ".burning.", ".compressing.")) and c.stat().st_size > 1024 * 1024]
                if cands and _label_shared(lbl0):
                    tok0 = _path_to_quality.get(str(all_eps[gi0 - 1].resolve()), "")
                    if tok0:
                        cands = [c for c in cands if f".{tok0}." in c.name]
                if cands:
                    found0 = cands[0]
            if found0 is None:
                legacy0 = d0 / f"{series_safe}_EP{gi0:03d}_AR.mp4"
                if legacy0.exists() and legacy0.stat().st_size > 1024 * 1024:
                    found0 = legacy0
            if found0 is not None:
                skip_set.add(gi0)
                rendered[_render_key(gi0)] = str(found0)
        if skip_set:
            push(f"⏭️ {len(skip_set)} حلقة جاهزة مسبقاً — هتتخطى من غير تجهيز.")

        # ═══ الترجمة اليدوية لحلقات بعينها: أرقامها بتتحدد قبل التشغيل، وملفاتها بتترفع من الواجهة ═══
        manual_set: set = set()
        manual_by_path = {}
        arabic_ready = set()
        if downloaded_manifest is not None:
            for index, path in enumerate(all_eps, 1):
                choice = (subtitle_choices or {}).get(str(path.resolve()), {})
                if choice.get("mode") == "ترجمة يدوية":
                    manual_set.add(index)
                    manual_by_path[str(path.resolve())] = choice["subtitle"]
                else:
                    track = _wizard_arabic_track(path)
                    if track:
                        exported_ar = _wizard_export_track(path, track)
                        if verify_full_dialogue_sub(exported_ar, log_cb=push):
                            manual_set.add(index)
                            manual_by_path[str(path.resolve())] = exported_ar
                            arabic_ready.add(str(path.resolve()))
                            push(f"✓ {path.name}: ترجمة عربية جاهزة (مسار حوار كامل)، بدون DeepSeek، الصياغة الأصلية مع ستايلنا البصري.")
                        else:
                            push(f"ℹ️ {path.name}: المسار العربي مش حوار كامل — هنكمل بالترجمة العادية.")
        else:
            _mtxt = str(manual_eps_text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
            # نطاقات زي 3-5 أو 3~5 أو 3 الى 5، وبعدها أي رقم لوحده (3 / 03 / 003 كلهم نفس الحلقة)
            for _a, _b in re.findall(r"(\d+)\s*(?:-|–|—|~|الى|إلى|to)\s*(\d+)", _mtxt):
                _lo, _hi = sorted((int(_a), int(_b)))
                for _g in range(_lo, min(_hi, total) + 1):
                    if _g >= 1:
                        manual_set.add(_g)
            _mtxt_single = re.sub(r"(\d+)\s*(?:-|–|—|~|الى|إلى|to)\s*(\d+)", " ", _mtxt)
            for _tok in re.findall(r"\d+", _mtxt_single):
                _g = int(_tok)
                if 1 <= _g <= total:
                    manual_set.add(_g)
        if manual_set:
            push(f"✍️ ترجمة يدوية للحلقات: {', '.join(f'EP{g:03d}' for g in sorted(manual_set))} — الباقي ترجمة تلقائية. ارفع الملفات من قسم ✍️ الترجمة اليدوية.")

        manual_dir = RAW_CACHE_ROOT / "_manual_subs" / series_safe

        def _wait_manual_ass(gi_m: int, vid_m: Path, log_cb=None) -> Dict[str, Any]:
            """حلقة يدوية: تستنى ملف الترجمة المترجم من الواجهة، تحوّله ASS بنفس الستايل، وترجع prep جاهز.
            بتستنى من غير ما تحجب باقي الحلقات، وبتكتب في اللوج كل دقيقة — مفيش استنى صامت."""
            cb = log_cb if callable(log_cb) else push
            ep_label_m = _label_of(gi_m)
            work_dir_m = RAW_CACHE_ROOT / "_work" / series_safe / _work_name(gi_m)
            work_dir_m.mkdir(parents=True, exist_ok=True)
            manual_dir.mkdir(parents=True, exist_ok=True)
            # لو فيه ترجمة مدمجة: احفظ نسخة إنجليزية للتحميل قبل الاستنى — هو بيترجم منها برا
            try:
                probe_m = probe_embedded_subtitles(str(vid_m))
                if probe_m is not None:
                    src_m = extract_embedded_subtitle(str(vid_m), probe_m[0], probe_m[1], str(work_dir_m))
                    en_dir_m = series_root / "_subs_en"
                    en_dir_m.mkdir(parents=True, exist_ok=True)
                    keep_m = en_dir_m / f"{series_dot}.{ep_label_m}.en{Path(src_m).suffix}"
                    shutil.copy2(src_m, str(keep_m))
                    cb(f"💾 {ep_label_m}: الترجمة الإنجليزية محفوظة للتحميل: {keep_m.name} — ترجمها وارفعها من قسم ✍️ الترجمة اليدوية.")
            except Exception as ex_m:
                cb(f"⚠️ {ep_label_m}: تعذر سحب ترجمة مدمجة للترجمة اليدوية ({ex_m}) — ارفع الملف المترجم جاهز.")
            cb(f"⏸️ {ep_label_m}: ترجمة يدوية — مستني ملف الترجمة المترجم. ارفعه من قسم ✍️ الترجمة اليدوية في الواجهة (رقم الحلقة {gi_m}).")
            selected_sub = manual_by_path.get(str(vid_m.resolve()))
            target: Optional[Path] = Path(selected_sub) if selected_sub else None
            last_ping = 0.0
            while target is None:
                for ext in (".srt", ".ass"):
                    cand = manual_dir / f"{ep_label_m}{ext}"
                    if cand.is_file() and cand.stat().st_size > 10:
                        target = cand
                        break
                if target is None:
                    if time.monotonic() - last_ping >= 60:
                        last_ping = time.monotonic()
                        cb(f"⌛ {ep_label_m}: لسه مستني ملف الترجمة اليدوي ({ep_label_m}.srt أو .ass)...")
                    time.sleep(2)
            cb(f"✓ {ep_label_m}: استلمت ملف الترجمة اليدوي: {target.name}")
            entries_m = parse_source_subtitles(str(target))
            if not entries_m:
                raise ValueError(f"ملف الترجمة اليدوي للحلقة {gi_m} فاضي أو غير مقروء.")
            translations_m = {e.index: getattr(e, "plain_text", "") for e in entries_m}
            vw_m, vh_m, _, _ = probe_video_info(str(vid_m))
            ep_ass_m = ass_root / f"{branded_stem(f'{series_dot}.{ep_label_m}', quality_token(vh_m))}.ass"
            write_responsive_ass(
                entries=entries_m,
                translations=translations_m,
                out_ass_path=str(ep_ass_m),
                target_w=vw_m,
                target_h=vh_m,
                style_preset=style_preset,
                credits=credits_dict if has_credits else None,
                wrap_mode=wrap_mode,
                soften_exclaim=soften_exclaim,
                preserve_wording=downloaded_manifest is not None,
            )
            cb(f"✓ {ep_label_m}: ملف الـ ASS من الترجمة اليدوية جاهز للحرق.")
            return {"ep_label": ep_label_m, "work_dir": work_dir_m, "entries": entries_m, "ep_ass": str(ep_ass_m)}

        def _next_pending(start_gi: int) -> int:
            for gj in range(start_gi, total + 1):
                if gj not in skip_set and gj not in manual_set:
                    return gj
            return 0

        def _prep_episode(gi_p: int, vid_p: Path, log_cb=None) -> Dict[str, Any]:
            """تجهيز حلقة كاملة قبل الحرق: سحب ترجمة/تفريغ ← ترجمة ← قاموس ← ملف ASS.
            بتشتغل إما في الخيط الرئيسي (خطة بديلة) أو في خيط الخلفية الواحد."""
            cb = log_cb if callable(log_cb) else push
            ep_label_p = _label_of(gi_p)
            work_dir_p = RAW_CACHE_ROOT / "_work" / series_safe / _work_name(gi_p)
            work_dir_p.mkdir(parents=True, exist_ok=True)

            source_sub_p = None
            is_raw_whisper_p = False
            if sub_mode.startswith("Auto"):
                probe = probe_embedded_subtitles(str(vid_p))
                if probe is not None:
                    source_sub_p = extract_embedded_subtitle(str(vid_p), probe[0], probe[1], str(work_dir_p))
                    cb(f"✓ {ep_label_p}: تم سحب الترجمة المدمجة ({probe[1]}).")
                    # نسخة دائمة من الترجمة الإنجليزية للتحميل (ترجمة خارجية) — النسخة الأصلية تتمسح مع فولدر العمل
                    try:
                        en_dir_p = series_root / "_subs_en"
                        en_dir_p.mkdir(parents=True, exist_ok=True)
                        keep_p = en_dir_p / f"{series_dot}.{ep_label_p}.en{Path(source_sub_p).suffix}"
                        shutil.copy2(source_sub_p, str(keep_p))
                        cb(f"💾 {ep_label_p}: الترجمة الإنجليزية محفوظة للتحميل: {keep_p.name}")
                    except Exception as keep_exc:
                        cb(f"⚠️ {ep_label_p}: تعذر حفظ نسخة الترجمة الإنجليزية للتحميل: {keep_exc}")
            if source_sub_p is None:
                is_raw_whisper_p = True
                source_sub_p = transcribe_with_whisper(
                    str(vid_p),
                    str(work_dir_p / "whisper.srt"),
                    model_size=whisper_model,
                    log_cb=cb,
                )

            entries_p = parse_source_subtitles(source_sub_p)

            if (api_key.strip() or "Local" in provider) and not glossary_box["val"]:
                glossary_box["val"] = load_or_extract_series_glossary(
                    series_name=series_name,
                    entries=entries_p,
                    is_raw_whisper=is_raw_whisper_p,
                    provider=_provider_key(provider),
                    api_key=api_key,
                    model_name=llm_model,
                    base_url=base_url,
                    log_cb=cb,
                )

            reused_p: Dict[int, str] = {}
            if tmem:
                for e in entries_p:
                    if getattr(e, "keep_untranslated", False):
                        continue
                    k = _tkey(getattr(e, "plain_text", ""))
                    if k and k in tmem:
                        reused_p[e.index] = tmem[k]
            if reused_p:
                cb(f"♻️ {ep_label_p}: {len(reused_p)} سطر مكرر (أوبنينج/إندينج) أُعيد استخدام ترجمته — المتبقي {len(entries_p) - len(reused_p)} سطر.")

            translations_p = translate_entries(
                entries=[e for e in entries_p if e.index not in reused_p],
                provider=_provider_key(provider),
                api_key=api_key,
                model_name=llm_model,
                base_url=base_url,
                series_name=series_name,
                glossary=glossary_box["val"],
                checkpoint_path=str(work_dir_p / "translation_checkpoint.json"),
                log_cb=cb,
            )
            translations_p.update(reused_p)

            idx2key_p = {
                e.index: _tkey(getattr(e, "plain_text", ""))
                for e in entries_p
                if not getattr(e, "keep_untranslated", False)
            }
            for _idx, _ar in translations_p.items():
                _k = idx2key_p.get(_idx)
                if _k:
                    tmem[_k] = _ar
            try:
                _tmp_tmem = tmem_path.with_suffix(".tmp")
                _tmp_tmem.write_text(json.dumps(tmem, ensure_ascii=False), encoding="utf-8")
                os.replace(str(_tmp_tmem), str(tmem_path))  # كتابة ذرّية: رفع Drive ممكن يشتغل بالتوازي
            except Exception:
                pass

            glossary_box["val"] = learn_glossary_from_translation(
                series_name=series_name,
                entries=entries_p,
                translations=translations_p,
                glossary=glossary_box["val"],
                provider=_provider_key(provider),
                api_key=api_key,
                model_name=llm_model,
                base_url=base_url,
                log_cb=cb,
            )

            vw_p, vh_p, _, _ = probe_video_info(str(vid_p))
            ep_ass_p = ass_root / f"{branded_stem(f'{series_dot}.{ep_label_p}', quality_token(vh_p))}.ass"
            write_responsive_ass(
                entries=entries_p,
                translations=translations_p,
                out_ass_path=str(ep_ass_p),
                target_w=vw_p,
                target_h=vh_p,
                style_preset=style_preset,
                credits=credits_dict if has_credits else None,
                wrap_mode=wrap_mode,
                soften_exclaim=soften_exclaim,
            )
            return {
                "ep_label": ep_label_p,
                "work_dir": work_dir_p,
                "entries": entries_p,
                "ep_ass": str(ep_ass_p),
            }

        def _bg_log(m) -> None:
            s = str(m)
            if s.startswith("[whisper_prog]"):
                return  # نبضات التفريغ اللحظية بتتخطى في وضع الخلفية
            push(s)

        prep_box: Dict[str, Any] = {}

        def _start_bg_prep(gi_p: int) -> None:
            if gi_p <= 0 or gi_p > total:
                return
            if prep_box.get("thread") is not None:
                return  # فيه تجهيز شغال أصلاً — التجهيز دايماً خيط واحد في نفس اللحظة
            vid_p = all_eps[gi_p - 1]
            prep_box.clear()
            prep_box["gi"] = gi_p

            def _bg() -> None:
                try:
                    prep_box["result"] = _prep_episode(gi_p, vid_p, _bg_log)
                except BaseException as exc_p:
                    prep_box["error"] = exc_p

            t = threading.Thread(target=_bg, daemon=True)
            prep_box["thread"] = t
            push(f"🧵 تجهيز مبكر: الحلقة {gi_p} من {total} بدأت تتجهز في الخلفية (ترجمة/تفريغ) أثناء شغل الحالية.")
            t.start()

        # ═══ وضع التوازي: حتى N حلقات مع بعض (التجهيز/الترجمة/التفريغ خيط واحد دايماً) ═══
        conc_n = max(1, min(10, int(concurrency or 3)))
        pending_gis = [g for g in range(1, total + 1) if g not in skip_set]

        def _grind_parallel():
            nonlocal last_ass
            push(f"🚀 وضع التوازي مفعّل: {conc_n} حلقات مع بعض — الترجمة والتفريغ واحدة ورا التانية عشان الذاكرة والقاموس.")
            free_b = shutil.disk_usage(str(STORAGE_ROOT)).free
            try:
                est_b = sum(all_eps[g - 1].stat().st_size for g in pending_gis[:conc_n] if all_eps[g - 1].exists())
            except Exception:
                est_b = 0
            need_b = est_b + conc_n * 1300 * 1024 * 1024  # خام + ناتج + مساحة عمل لكل حلقة متزامنة
            disk_warn = ""
            if free_b < need_b:
                disk_warn = (
                    f"المساحة الفاضية ({free_b / 1e9:.1f}GB) أقل من المطلوب تقريباً لتشغيل {conc_n} حلقات مع بعض "
                    f"({need_b / 1e9:.1f}GB) — قلّل العدد أو نظّف البود."
                )
                push(f"⚠️ {disk_warn}")

            work_q: "queue.Queue[int]" = queue.Queue()
            for g in pending_gis:
                work_q.put(g)
            cards_box: Dict[int, Dict[str, Any]] = {}
            prep_results: Dict[int, Dict[str, Any]] = {}
            prep_lock = threading.Lock()   # التجهيز (سحب ترجمة/تفريغ/ترجمة) واحد في اللحظة
            tg_lock = threading.Lock()     # رفع تيليجرام واحد في اللحظة مع الفاصل الهادي
            state_lock = threading.Lock()  # رفع ملفات الحالة الصغيرة على Drive واحد في اللحظة

            def _worker() -> None:
                while True:
                    try:
                        gi = work_q.get_nowait()
                    except queue.Empty:
                        return
                    vid = all_eps[gi - 1]
                    ep_label = _label_of(gi)
                    card: Dict[str, Any] = {"ep": ep_label, "name": f"{series_dot}.{ep_label}", "step": "في الطابور", "pct": 0.0, "frame": "", "mode": "cyan"}
                    cards_box[gi] = card
                    try:
                        try:
                            _, _vh0, _, _ = probe_video_info(str(vid))
                        except Exception:
                            _vh0 = 0
                        out_mp4 = part_dir_for(gi) / (branded_stem(f"{series_dot}.{ep_label}", quality_token(_vh0)) + ".mp4")
                        push(f"━━━━━━ {ep_label} ({gi}/{total}): {vid.name} ━━━━━━")
                        if gi in manual_set:
                            card["step"] = "مستني ملف الترجمة اليدوي"
                            prep = _wait_manual_ass(gi, vid, _bg_log)  # من غير prep_lock: مفيش ترجمة AI ولا ذاكرة هنا
                        else:
                            card["step"] = "تجهيز الترجمة"
                            with prep_lock:
                                prep = _prep_episode(gi, vid, _bg_log)
                        prep_results[gi] = prep
                        card["pct"] = 5.0

                        frame_ts = {"t": 0.0}

                        def _burn_prog(p, met, det, _c=card, _vid=vid):
                            _c["pct"] = 5.0 + float(p) * 0.75
                            _c["step"] = str(met)
                            now_m = time.monotonic()
                            if now_m - frame_ts["t"] >= 4.0:
                                frame_ts["t"] = now_m
                                f = extract_card_frame(str(_vid), float(p) / 100.0)
                                if f:
                                    _c["frame"] = f

                        burn_episode(
                            video_path=str(vid),
                            ass_path=str(prep["ep_ass"]),
                            output_path=str(out_mp4),
                            fonts_dir=str(FONTS_DIR),
                            codec=codec,
                            preset=preset,
                            x265_preset=x265_preset,
                            audio_bitrate=audio_bitrate,
                            logo_path=str(LOGO_FILE) if LOGO_FILE.is_file() else "",
                            logo_position=logo_position,
                            logo_opacity=float(logo_opacity),
                            logo_scale=float(logo_scale),
                            log_cb=push,
                            progress_cb=_burn_prog,
                        )
                        card["pct"] = 82.0
                        card["step"] = "فحص الجودة"
                        qc_burned_episode(
                            str(vid), str(out_mp4),
                            ass_path=str(prep["ep_ass"]),
                            expected_lines=len(prep["entries"]),
                            qc_dir=str(series_root / "_qc" / _work_name(gi)),
                            log_cb=push,
                            progress_cb=lambda p, m, d: card.update({"pct": 82.0 + float(p) * 0.06}),
                        )
                        rendered[_render_key(gi, _vh0)] = str(out_mp4)
                        if drive_ok:
                            try:
                                card["step"] = "نسخ احتياطي Drive"
                                upload_to_rclone(
                                    str(out_mp4), drv_remote, f"{drv_folder}/{series_safe}",
                                    generate_link=False, log_cb=push,
                                    progress_cb=lambda p, m, d: card.update({"pct": 88.0 + float(p) * 0.06}),
                                )
                                with state_lock:
                                    for _sf, _sub in ((tmem_path, f"_state/{series_safe}"), (_glossary_dir() / f"{_glossary_slug(series_name)}.json", "_state/glossaries")):
                                        if _sf.is_file():
                                            upload_to_rclone(str(_sf), drv_remote, f"{drv_folder}/{_sub}", generate_link=False, log_cb=push)
                            except Exception as up_exc:
                                push(f"⚠️ فشل النسخ الاحتياطي لـ {ep_label} على Drive: {up_exc} — الحلقة محفوظة محلياً ومكملين.")
                        if enable_telegram:
                            if not tgchan or not tgchan.is_ready():
                                if not tg_warned["v"]:
                                    tg_warned["v"] = True
                                    push("⚠️ الرفع على تيليجرام متفعّل بس تيليجرام مش جاهز — سجّل الدخول وأنشئ القناة من إعدادات 📡 تيليجرام. هيتم تخطي رفع تيليجرام للموسم ده.")
                            else:
                                try:
                                    card["step"] = "رفع تيليجرام"
                                    with tg_lock:
                                        tgchan.upload_episode(series_name, _ep_num_of(gi, ep_label), str(out_mp4), log_cb=push)
                                        time.sleep(tgchan.UPLOAD_GAP_SECONDS)
                                except Exception as tg_exc:
                                    push(f"⚠️ فشل رفع {ep_label} على تيليجرام: {tg_exc} — الحلقة محفوظة ومكملين.")
                        shutil.rmtree(str(prep["work_dir"]), ignore_errors=True)
                        if cleanup_temp and vid.resolve().is_relative_to(RAW_CACHE_ROOT.resolve()):
                            try:
                                vid.unlink(missing_ok=True)
                            except OSError:
                                pass
                        card["pct"] = 100.0
                        card["step"] = "خلصت"
                        card["mode"] = "done"
                        push(f"✅ {ep_label} جاهزة: {out_mp4.name}")
                    except Exception as ep_exc:
                        card["mode"] = "error"
                        card["step"] = "فشلت"
                        failed.append(f"{ep_label} ({vid.name}): {ep_exc}")
                        push(f"❌ فشلت {ep_label}: {ep_exc} — متابعة باقي الحلقات...")

            threads = [threading.Thread(target=_worker, daemon=True) for _ in range(min(conc_n, len(pending_gis)))]
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                live_cards = [cards_box[g] for g in sorted(cards_box)]
                queued_names = [_label_of(g) for g in pending_gis if g not in cards_box][:conc_n]
                yield pack(render_multi_hud(
                    live_cards, queued_names, str(queue_layout),
                    len(rendered) - len(skip_set), len(failed), total - len(skip_set), conc_n, disk_warn,
                ))
                time.sleep(0.6)
            for t in threads:
                t.join()
            if prep_results:
                last_ass = str(prep_results[max(prep_results)]["ep_ass"])
            push(f"🏁 وضع التوازي خلص: {len(rendered) - len(skip_set)} حلقة اتعملت في الجولة دي، {len(failed)} فشلت.")

        parallel_done = False
        if conc_n > 1 and len(pending_gis) > 1:
            yield from _grind_parallel()
            parallel_done = True

        # ابدأ تجهيز أول حلقة ناقصة فوراً
        _first_pending = 0 if parallel_done else _next_pending(1)
        if _first_pending:
            _start_bg_prep(_first_pending)

        # الوضع المتتابع: الحلقات التلقائية الأول بالترتيب، واليدوية تتأجل للآخر عشان مستناش غيرها
        _seq_order = [(g, all_eps[g - 1]) for g in range(1, total + 1) if g not in manual_set] + [
            (g, all_eps[g - 1]) for g in sorted(manual_set)
        ]
        for gi, vid in (_seq_order if not parallel_done else []):
            ep_label = _label_of(gi)
            grind_ctx["gi"] = gi

            if gi in skip_set:
                push(f"⏭️ {ep_label} جاهزة مسبقاً — تخطي ({Path(rendered[_render_key(gi)]).name})")
                continue

            try:
                _, _vh0, _, _ = probe_video_info(str(vid))
            except Exception:
                _vh0 = 0
            out_mp4 = part_dir_for(gi) / (branded_stem(f"{series_dot}.{ep_label}", quality_token(_vh0)) + ".mp4")

            push(f"━━━━━━ {ep_label} ({gi}/{total}): {vid.name} ━━━━━━")

            try:
                # استهلاك التجهيز المبكر للحلقة دي (مع الانتظار لو لسه شغال)
                prep = None
                if gi in manual_set:
                    prep = yield from run_live(
                        _wait_manual_ass,
                        gi, vid,
                        badge=f"MANUAL SUB [{ep_label} {gi}/{total}]",
                        title=f"الحلقة {gi} من {total}: ترجمة يدوية — مستنية رفع الملف من قسم ✍️ الترجمة اليدوية...",
                        accepts_progress_cb=False,
                    )
                    push(f"✓ {ep_label}: ملف الترجمة اليدوي اتقري وجاهز للحرق.")
                pt = prep_box.get("thread")
                if pt is not None:
                    same = prep_box.get("gi") == gi
                    if same and pt.is_alive():
                        push(f"⏳ الشغل الحالي خلص قبل تجهيز {ep_label} — مستنيين الترجمة تكمل...")
                        while pt.is_alive():
                            last_log = logs[-1] if logs else "..."
                            yield pack(render_progress_hud(
                                0.0,
                                f"PREP [{ep_label} {gi}/{total}]",
                                f"تجهيز الحلقة {gi} من {total}: سحب ترجمة/تفريغ + ترجمة بالذكاء الاصطناعي...",
                                str(last_log)[-110:],
                                "الخلفية شغالة — أول ما تخلص يبدأ الحرق فوراً",
                                mode="cyan",
                                overall_pct=((gi - 1) / total) * 100.0,
                                grind_label=f"طحن الموسم: {gi - 1} من {total}",
                            ))
                            time.sleep(0.4)
                    pt.join()
                    if same and prep_box.get("error") is None and prep_box.get("result"):
                        prep = prep_box["result"]
                        push(f"⚡ {ep_label}: التجهيز المبكر جاهز — الحرق هيبدأ على طول من غير انتظار.")
                    elif same and prep_box.get("error") is not None:
                        push(f"⚠️ التجهيز المبكر لـ {ep_label} فشل: {prep_box['error']} — هنجهزها دلوقتي بالطريقة العادية.")
                    prep_box.clear()

                if prep is None:
                    # خطة بديلة: تجهيز مباشر في الخيط الرئيسي (أول مرة أو بعد فشل التجهيز المبكر)
                    prep = yield from run_live(
                        _prep_episode,
                        gi, vid,
                        badge=f"PREP [{ep_label} {gi}/{total}]",
                        title=f"تجهيز الحلقة {gi} من {total}: سحب ترجمة/تفريغ + ترجمة...",
                        accepts_progress_cb=False,
                    )
                    push(f"✓ {ep_label}: الترجمة وملف الـ ASS جاهزين.")

                last_ass = str(prep["ep_ass"])

                # ابدأ تجهيز الحلقة اللي بعدها في الخلفية قبل ما الحرق يبدأ
                _start_bg_prep(_next_pending(gi + 1))

                burn_vid = str(vid)

                yield from run_live(
                    burn_episode,
                    video_path=burn_vid,
                    ass_path=str(prep["ep_ass"]),
                    output_path=str(out_mp4),
                    fonts_dir=str(FONTS_DIR),
                    codec=codec,
                    preset=preset,
                    x265_preset=x265_preset,
                    audio_bitrate=audio_bitrate,
                    logo_path=str(LOGO_FILE) if LOGO_FILE.is_file() else "",
                    logo_position=logo_position,
                    logo_opacity=float(logo_opacity),
                    logo_scale=float(logo_scale),
                    badge=f"BURN [{ep_label} {gi}/{total}]",
                    title=f"حرق الحلقة {gi} من {total} ({codec})...",
                    accepts_progress_cb=True,
                )
                yield from run_live(
                    qc_burned_episode,
                    burn_vid, str(out_mp4),
                    ass_path=str(prep["ep_ass"]),
                    expected_lines=len(prep["entries"]),
                    qc_dir=str(series_root / "_qc" / _work_name(gi)),
                    badge=f"QC [{ep_label} {gi}/{total}]",
                    title=f"فحص جودة حلقة {gi} من {total} بعد الحرق...",
                    accepts_progress_cb=True,
                )
                rendered[_render_key(gi, _vh0)] = str(out_mp4)
                if drive_ok:
                    try:
                        yield from run_live(
                            upload_to_rclone,
                            str(out_mp4), drv_remote, f"{drv_folder}/{series_safe}",
                            generate_link=False,
                            badge=f"DRIVE BACKUP [{ep_label} {gi}/{total}]",
                            title=f"نسخ احتياطي لحلقة {gi} من {total} على Google Drive...",
                            accepts_progress_cb=True,
                        )
                        for _sf, _sub in ((tmem_path, f"_state/{series_safe}"), (_glossary_dir() / f"{_glossary_slug(series_name)}.json", "_state/glossaries")):
                            if _sf.is_file():
                                yield from run_live(
                                    upload_to_rclone,
                                    str(_sf), drv_remote, f"{drv_folder}/{_sub}",
                                    generate_link=False,
                                    badge=f"STATE BACKUP [{ep_label}]",
                                    title="حفظ ذاكرة الترجمة والقاموس على Drive...",
                                    accepts_progress_cb=False,
                                )
                    except Exception as up_exc:
                        push(f"⚠️ فشل النسخ الاحتياطي لـ {ep_label} على Drive: {up_exc} — الحلقة محفوظة محلياً ومكملين.")
                if enable_telegram:
                    if not tgchan or not tgchan.is_ready():
                        if not tg_warned["v"]:
                            tg_warned["v"] = True
                            push("⚠️ الرفع على تيليجرام متفعّل بس تيليجرام مش جاهز — سجّل الدخول وأنشئ القناة من إعدادات 📡 تيليجرام. هيتم تخطي رفع تيليجرام للموسم ده.")
                    else:
                        try:
                            yield from run_live(
                                tgchan.upload_episode, series_name, _ep_num_of(gi, ep_label), str(out_mp4),
                                badge=f"TELEGRAM [{ep_label} {gi}/{total}]",
                                title=f"رفع حلقة {gi} من {total} على قناة تيليجرام الخاصة...",
                                accepts_progress_cb=True,
                            )
                            time.sleep(tgchan.UPLOAD_GAP_SECONDS)  # فاصل هادي عشان تيليجرام ما يقيّدش الحساب
                        except Exception as tg_exc:
                            push(f"⚠️ فشل رفع {ep_label} على تيليجرام: {tg_exc} — الحلقة محفوظة ومكملين.")
                shutil.rmtree(str(prep["work_dir"]), ignore_errors=True)
                if cleanup_temp and vid.resolve().is_relative_to(RAW_CACHE_ROOT.resolve()):
                    try:
                        vid.unlink(missing_ok=True)
                    except OSError:
                        pass
            except Exception as ep_exc:
                failed.append(f"{ep_label} ({vid.name}): {ep_exc}")
                push(f"❌ فشلت {ep_label}: {ep_exc} — متابعة باقي الحلقات...")
                continue

        all_local = list_local_outputs()
        # ═══ ملفات الترجمة الإنجليزية للترجمة الخارجية: فردية + أرشيف واحد للكل ═══
        en_files: List[str] = []
        en_dir = series_root / "_subs_en"
        if en_dir.is_dir():
            en_files = sorted(str(p) for p in list(en_dir.glob("*.srt")) + list(en_dir.glob("*.ass")))
            if len(en_files) > 1:
                try:
                    zip_path = shutil.make_archive(str(series_root / "_subs_en_all"), "zip", root_dir=str(en_dir))
                    en_files.append(zip_path)
                    push(f"📦 {len(en_files) - 1} ملف ترجمة إنجليزية جاهزين للتحميل + أرشيف واحد يجمعهم.")
                except Exception as zip_exc:
                    push(f"⚠️ تعذر تجميع ملفات الترجمة الإنجليزية في أرشيف: {zip_exc} — الملفات الفردية متاحة.")
            elif en_files:
                push("📄 ملف الترجمة الإنجليزية جاهز للتحميل من قسم التحميلات.")
        first_video = next(iter(rendered.values()), None)
        preview_file = first_video
        if first_video:
            try:
                preview_file = yield from run_live(
                    make_browser_preview,
                    first_video,
                    str(Path(first_video).with_name(Path(first_video).stem + "_preview.mp4")),
                    badge="PREVIEW · H264",
                    title="تجهيز نسخة معاينة سريعة للمتصفح (H.264 على كرت الشاشة)...",
                    accepts_progress_cb=True,
                )
            except Exception as pv_exc:
                push(f"⚠️ تعذر تجهيز نسخة المعاينة السريعة: {pv_exc} — المعاينة هتستخدم الملف الأصلي.")
                preview_file = first_video
        done_n, fail_n = len(rendered), len(failed)

        if fail_n:
            push(f"⚠️ حلقات فشلت ({fail_n}):")
            for f_ in failed:
                push(f"   • {f_}")

        push(f"✅ اكتمل الموسم! {done_n}/{total} حلقة جاهزة في: {series_root}")
        done_hud = render_progress_hud(
            100.0 if not fail_n else 97.0,
            "SEASON COMPLETE" if not fail_n else "SEASON DONE WITH ERRORS",
            f"تم إنتاج {done_n} من {total} حلقة" + (f" · {fail_n} فشلت" if fail_n else " بنجاح كامل!"),
            f"الفولدر: {series_root}" + (" · مقسم ×35" if split_parts else ""),
            f"{done_n} MP4",
            mode="done" if not fail_n else "error",
            overall_pct=(done_n / total) * 100.0,
            grind_label=f"طحن الموسم: {done_n} من {total}",
        )
        # نفس فصل التحديثات: لوحة الإتمام وزرار [S-PORTAL] فوراً بدون انتظار تحويل المعاينة
        yield pack(
            done_hud,
            local_dd=gr.update(choices=all_local),
            raw_dd=gr.update(choices=[""] + list_cached_raw_videos()),
            vids_val=list(rendered.values())[:40],
            ass_val=last_ass,
            state_val={"folder": str(series_root), "files": rendered},
            g2=gr.update(visible=True),
            dls=gr.update(visible=True),
            subs=(en_files if en_files else gr.update()),
            arch=gr.update(visible=(done_n > 1)),
            arch_txt=gr.update(value=f'<div style="color:#eaf6ff;font-size:16px;font-weight:600;padding:2px 0 8px;">الدفعة خلصت ({done_n} حلقة). تجمعهم في أرشيف واحد؟</div>') if done_n > 1 else gr.update(),
        )
        yield pack(
            done_hud,
            preview_val=preview_file,
            local_dd=gr.update(value=first_video),
        )
    except Exception as exc:
        push(f"❌ خطأ عام في وضع الموسم: {exc}\n{traceback.format_exc()[-1500:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        try:
            err_hud = render_progress_hud(
                0.0,
                "ERROR · SEASON HALTED",
                f"توقف الموسم بسبب خطأ: {str(exc)[:110]}",
                "الحلقات الجاهزة ونقاط الحفظ محفوظة — أعد التشغيل للاستكمال",
                "راجع سجل العمليات",
                mode="error",
            )
        except Exception:
            err_hud = ""
        try:
            raw_choices = [""] + list_cached_raw_videos()
        except Exception:
            raw_choices = [""]
        yield pack(
            err_hud,
            raw_dd=gr.update(choices=raw_choices),
            state_val={},
            g1=gr.update(visible=True),
        )
    except BaseException as exc:
        push(f"🛑 توقف مفاجئ خارج معالجة الأخطاء في وضع الموسم ({type(exc).__name__}): {exc}\n{traceback.format_exc()[-800:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        raise


def step2_approve_and_upload(
    job_state: Dict,
    enable_dood: bool, dood_api_key: str,
    enable_gdrive: bool, gdrive_remote: str, gdrive_folder: str,
    enable_telegram: bool = False, series_name: str = "", series_pick: str = "",
    status_box: Optional[Dict] = None,
):
    def _set_state(value: str) -> None:
        if status_box is not None:
            status_box["state"] = value

    _set_state("none" if not (enable_dood or enable_gdrive or enable_telegram) else "pending")
    if not job_state or not job_state.get("files"):
        _set_state("failed")
        yield (
            render_progress_hud(0.0, "S-PORTAL · NO FILES", "شغّل المرحلة [STRIKE] للمعاينة أولاً قبل الرفع.", "—", "—", mode="error"),
            "⚠️ لا توجد فيديوهات جاهزة للرفع حالياً.",
            "",
        )
        return

    logs: List[str] = []
    links_report: List[str] = [f"📁 مسار الحفظ المحلي على RunPod:\n  {job_state['folder']}\n"]

    def push(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        logs.append(line)
        _append_disk_log(line)
        print(line, flush=True)

    push(f"🗂️ نسخة دائمة من هذا السجل تُحفظ تلقائياً في: {RUN_LOG_FILE}")

    def run_upload_live(fn: Callable, *args, badge: str, title: str, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except BaseException as e:
                res_box["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        cur_pct, cur_met, cur_det = 1.0, "جاري الاتصال بالسحابة...", ""
        dirty = True
        last_y = 0.0
        while t.is_alive() or not ev_q.empty():
            try:
                items = [ev_q.get(timeout=0.25)]
            except queue.Empty:
                items = []
            while not ev_q.empty():
                try:
                    items.append(ev_q.get_nowait())
                except queue.Empty:
                    break
            for k, p in items:
                dirty = True
                if k == "prog":
                    cur_pct, cur_met, cur_det = p  # type: ignore[misc]
                else:
                    push(str(p))
            now_m = time.monotonic()
            if dirty and (now_m - last_y >= 0.35 or not t.is_alive()):
                last_y = now_m
                dirty = False
                yield (
                    render_progress_hud(cur_pct, badge, title, cur_met, cur_det, mode="violet"),
                    "\n".join(logs[-150:]),
                    "\n".join(links_report),
                )
        if "err" in res_box:
            raise res_box["err"]  # type: ignore[misc]
        return res_box.get("val")

    try:
        for q_label, mp4_path in job_state["files"].items():
            links_report.append(f"=== جودة {q_label} ({Path(mp4_path).name}) ===")
            links_report.append(f"  Local Path: {mp4_path}")

            if enable_dood:
                dood_res = yield from run_upload_live(
                    upload_to_doodstream, mp4_path, dood_api_key,
                    badge=f"S-PORTAL · DOODSTREAM [{q_label}]", title=f"جاري رفع نسخة {q_label} على DoodStream...",
                )
                links_report.append(f"  DoodStream Watch: {dood_res['watch_url']}")
                links_report.append(f"  DoodStream Embed: {dood_res['embed_url']}")

            if enable_gdrive:
                gd_res = yield from run_upload_live(
                    upload_to_rclone, mp4_path, gdrive_remote, gdrive_folder, generate_link=False,
                    badge=f"S-PORTAL · GOOGLE DRIVE [{q_label}]", title=f"جاري رفع نسخة {q_label} على Google Drive...",
                )
                links_report.append(f"  Google Drive: {gd_res}")
            links_report.append("")

            if enable_telegram:
                if not tgchan:
                    _set_state("failed")
                    push("⚠️ موديول تيليجرام مش متاح (telegram_channel.py) — اتخطى الرفع على تيليجرام.")
                elif not tgchan.is_ready():
                    _set_state("failed")
                    push("⚠️ تيليجرام مش جاهز — سجّل الدخول وأنشئ القناة من إعدادات 📡 تيليجرام. اتخطى الرفع على تيليجرام.")
                else:
                    # كل الجودات الجاهزة بتترفع (1080p و720p)، مش أول ملف بس
                    try:
                        ep_n = int(extract_episode_number(Path(mp4_path).name) or 0)
                    except Exception:
                        ep_n = 0
                    series_eff = (series_pick or "").strip()
                    if not series_eff or series_eff == SERIES_NEW_OPT:
                        series_eff = (series_name or "").strip()
                    if not series_eff:
                        stem0 = re.sub(r"\.\[Animerza\]$", "", Path(mp4_path).stem)
                        m = re.match(r"(.+?)[_.](?:S\d{1,2}E\d{1,4}|EP?\d{1,4})[_.].*", stem0) or re.match(r"(.+?)_EP\d+", stem0)
                        series_eff = (m.group(1).replace(".", " ").replace("_", " ").strip() if m else "Anime") or "Anime"
                    try:
                        tg_link = yield from run_upload_live(
                            tgchan.upload_episode, series_eff, ep_n, mp4_path,
                            badge=f"S-PORTAL · TELEGRAM [{q_label}]", title=f"رفع نسخة {q_label} على قناة تيليجرام الخاصة...",
                        )
                        links_report.append(f"  Telegram: {tg_link}")
                        time.sleep(tgchan.UPLOAD_GAP_SECONDS)  # فاصل هادي بين رفع نسخة والتانية
                    except Exception as tg_exc:
                        _set_state("failed")
                        push(f"⚠️ فشل رفع تيليجرام ({q_label}): {tg_exc} — الحلقة محفوظة محلياً ومكملين.")

        (Path(job_state["folder"]) / "links.txt").write_text("\n".join(links_report), encoding="utf-8")
        if status_box is not None and status_box.get("state") == "pending":
            status_box["state"] = "ok"
        yield (
            render_progress_hud(100.0, "S-PORTAL · UPLOAD COMPLETE", "اكتمل رفع جميع الجودات واستخراج الروابط!", "100% Cloud Sync", job_state["folder"], mode="done"),
            "\n".join(logs[-150:]),
            gr.update(value="\n".join(links_report), visible=True),
        )
    except Exception as exc:
        _set_state("failed")
        push(f"❌ خطأ أثناء الرفع: {exc}\n{traceback.format_exc()[-1500:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        try:
            err_hud = render_progress_hud(0.0, "S-PORTAL · ERROR", f"خطأ أثناء الرفع: {str(exc)[:100]}", "راجع السجل", "", mode="error")
        except Exception:
            err_hud = ""
        yield (
            err_hud,
            "\n".join(logs[-150:]),
            "\n".join(links_report),
        )
    except BaseException as exc:
        push(f"🛑 توقف مفاجئ أثناء الرفع ({type(exc).__name__}): {exc}\n{traceback.format_exc()[-800:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        raise



# ═══ أرشفة الحلقات الجاهزة (RAR/7z) ═══

def _build_blocks_kwargs() -> Dict:
    sig = inspect.signature(gr.Blocks.__init__).parameters
    kwargs: Dict = {"title": "سَبّر الأنمي | Anime Subber"}
    if "theme" in sig:
        kwargs["theme"] = gr.themes.Base(primary_hue="sky", secondary_hue="violet", neutral_hue="slate")
    if "css" in sig:
        kwargs["css"] = ""
    if "head" in sig:
        kwargs["head"] = ""
    return kwargs


IPPO_HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@500;700;800;900&family=Chakra+Petch:wght@600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">

<script>
(function initIppoRingCanvas() {
    function mountCanvas() {
        if (document.getElementById("ippo-bg-canvas")) return;
        const canvas = document.createElement("canvas");
        canvas.id = "ippo-bg-canvas";
        document.body.prepend(canvas);

        const ctx = canvas.getContext("2d");
        let w, h, t = 0;

        function resize() {
            w = canvas.width = window.innerWidth;
            h = canvas.height = window.innerHeight;
        }
        window.addEventListener("resize", resize);
        resize();

        // جزيئات غبار الطباشير والشرارات الهادية
        const particles = Array.from({ length: 45 }, () => ({
            x: Math.random() * w,
            y: Math.random() * h,
            r: Math.random() * 1.8 + 0.6,
            vx: (Math.random() - 0.5) * 0.25,
            vy: -(Math.random() * 0.35 + 0.1),
            type: Math.random() > 0.78 ? (Math.random() > 0.5 ? "red" : "green") : "chalk",
            alpha: Math.random() * 0.45 + 0.15
        }));

        function draw() {
            t += 0.008;
            ctx.clearRect(0, 0, w, h);

            // 1. أضواء كشافات الحلبة العلوية (حركة ناعمة جداً)
            const spot1X = w * 0.28 + Math.sin(t * 0.7) * 60;
            const spot2X = w * 0.72 + Math.cos(t * 0.6) * 60;

            const g1 = ctx.createRadialGradient(spot1X, -80, 10, spot1X, h * 0.45, Math.max(w, h) * 0.55);
            g1.addColorStop(0, "rgba(220, 38, 38, 0.14)");
            g1.addColorStop(0.5, "rgba(220, 38, 38, 0.04)");
            g1.addColorStop(1, "rgba(0, 0, 0, 0)");
            ctx.fillStyle = g1;
            ctx.fillRect(0, 0, w, h);

            const g2 = ctx.createRadialGradient(spot2X, -80, 10, spot2X, h * 0.45, Math.max(w, h) * 0.55);
            g2.addColorStop(0, "rgba(245, 158, 11, 0.10)");
            g2.addColorStop(0.5, "rgba(34, 197, 94, 0.03)");
            g2.addColorStop(1, "rgba(0, 0, 0, 0)");
            ctx.fillStyle = g2;
            ctx.fillRect(0, 0, w, h);

            // 2. حبال الحلبة الثلاثة في الخلفية
            const ropeY = [h * 0.30, h * 0.52, h * 0.74];
            ropeY.forEach((ry, idx) => {
                ctx.beginPath();
                ctx.moveTo(0, ry);
                ctx.quadraticCurveTo(w / 2, ry + Math.sin(t + idx) * 6, w, ry);
                ctx.strokeStyle = idx === 1 ? "rgba(220, 38, 38, 0.10)" : "rgba(255, 255, 255, 0.04)";
                ctx.lineWidth = idx === 1 ? 2 : 1.2;
                ctx.stroke();
            });

            // 3. موجة Dempsey Roll (شكل 8 انسيابي هادي)
            ctx.save();
            ctx.beginPath();
            const cx = w * 0.5, cy = h * 0.52;
            const scaleX = Math.min(w * 0.32, 340);
            const scaleY = Math.min(h * 0.16, 110);
            for (let a = 0; a <= Math.PI * 2; a += 0.08) {
                const px = cx + Math.sin(a + t) * scaleX;
                const py = cy + Math.sin(2 * (a + t)) * scaleY;
                if (a === 0) ctx.moveTo(px, py);
                else ctx.lineTo(px, py);
            }
            const waveGrad = ctx.createLinearGradient(cx - scaleX, cy, cx + scaleX, cy);
            waveGrad.addColorStop(0, "rgba(220, 38, 38, 0.09)");
            waveGrad.addColorStop(0.5, "rgba(245, 158, 11, 0.06)");
            waveGrad.addColorStop(1, "rgba(34, 197, 94, 0.09)");
            ctx.strokeStyle = waveGrad;
            ctx.lineWidth = 2;
            ctx.stroke();
            ctx.restore();

            // 4. غبار الطباشير والشرارات
            particles.forEach(p => {
                p.x += p.vx;
                p.y += p.vy;
                if (p.y < -10) { p.y = h + 10; p.x = Math.random() * w; }
                if (p.x < -10) p.x = w + 10;
                if (p.x > w + 10) p.x = -10;

                ctx.beginPath();
                ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
                if (p.type === "red") ctx.fillStyle = `rgba(239, 68, 68, ${p.alpha})`;
                else if (p.type === "green") ctx.fillStyle = `rgba(34, 197, 94, ${p.alpha * 0.9})`;
                else ctx.fillStyle = `rgba(226, 232, 240, ${p.alpha * 0.55})`;
                ctx.fill();
            });

            requestAnimationFrame(draw);
        }
        draw();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mountCanvas);
    } else {
        mountCanvas();
    }
})();
</script>
"""

IPPO_CSS = """
:root {
    --ippo-bg: #07070b;
    --ippo-surface: rgba(14, 14, 20, 0.88);
    --ippo-surface-solid: #0a0a0f;
    --ippo-red: #dc2626;
    --ippo-red-glow: rgba(220, 38, 38, 0.45);
    --ippo-gold: #f59e0b;
    --ippo-gold-glow: rgba(245, 158, 11, 0.35);
    --ippo-spirit: #22c55e;
    --ippo-border: rgba(255, 255, 255, 0.09);
    --ippo-text: #f8fafc;
    --ippo-muted: #94a3b8;
}

/* خلفية الصفحة والـ Canvas */
html, body, .gradio-container {
    background: var(--ippo-bg) !important;
    color: var(--ippo-text) !important;
    font-family: 'Cairo', sans-serif !important;
    direction: rtl !important;
    min-height: 100vh !important;
    overflow-x: hidden !important;
}

#ippo-bg-canvas {
    position: fixed !important;
    inset: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: 0 !important;
    pointer-events: none !important;
}

.gradio-container > .main,
.gradio-container .wrap,
.gradio-container .contain {
    position: relative !important;
    z-index: 2 !important;
    background: transparent !important;
    max-width: 860px !important;
    margin: 0 auto !important;
}

/* إخفاء فوتر Gradio */
footer { display: none !important; }

/* ====== لوحة نتائج الجولات (Korakuen Hall LED) ====== */
.ippo-scoreboard {
    background: linear-gradient(180deg, #101018 0%, #09090e 100%);
    border: 1px solid rgba(220, 38, 38, 0.35);
    border-radius: 16px;
    padding: 14px 18px;
    margin-bottom: 14px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.65), inset 0 1px 0 rgba(255, 255, 255, 0.05);
}

.ippo-rounds-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}

.ippo-round-node {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 14px;
    border-radius: 12px;
    background: #0c0c12;
    border: 1px solid var(--ippo-border);
    color: var(--ippo-muted);
    transition: all 0.3s ease;
    flex: 1;
    justify-content: center;
}

.ippo-round-num {
    font-family: 'Chakra Petch', sans-serif;
    font-weight: 700;
    font-size: 15px;
    width: 28px;
    height: 28px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #161622;
    color: var(--ippo-muted);
}

.ippo-round-node.active {
    border-color: var(--ippo-red);
    background: linear-gradient(135deg, rgba(220, 38, 38, 0.2), rgba(15, 15, 22, 0.95));
    color: #fff;
    box-shadow: 0 0 18px var(--ippo-red-glow);
}

.ippo-round-node.active .ippo-round-num {
    background: var(--ippo-red);
    color: #fff;
    box-shadow: 0 0 10px var(--ippo-red-glow);
}

.ippo-round-node.done {
    border-color: rgba(245, 158, 11, 0.45);
    color: #fde68a;
}

.ippo-round-node.done .ippo-round-num {
    background: var(--ippo-gold);
    color: #07070b;
}

/* ====== شريط Tale of the Tape ====== */
.ippo-tape-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: space-between;
    background: rgba(10, 10, 15, 0.85);
    border: 1px solid rgba(245, 158, 11, 0.25);
    border-right: 4px solid var(--ippo-gold);
    border-radius: 12px;
    padding: 10px 16px;
    margin-bottom: 16px;
    font-size: 13px;
}

.ippo-tape-item span {
    color: var(--ippo-muted);
    margin-left: 6px;
}

.ippo-tape-item strong {
    color: var(--ippo-gold);
    font-family: 'Chakra Petch', 'Cairo', sans-serif;
}

/* ====== بطاقة الخطوة الواحدة (Step Card) ====== */
.ippo-step-card {
    background: var(--ippo-surface) !important;
    backdrop-filter: blur(12px) !important;
    border: 1px solid rgba(220, 38, 38, 0.28) !important;
    border-radius: 18px !important;
    padding: 24px !important;
    box-shadow: 0 18px 45px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(255, 255, 255, 0.03) inset !important;
    animation: ippoSlideIn 0.35s cubic-bezier(0.22, 1, 0.36, 1) !important;
}

@keyframes ippoSlideIn {
    from { opacity: 0; transform: translateY(14px) scale(0.99); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
}

.ippo-step-title {
    font-size: 20px;
    font-weight: 800;
    color: #fff;
    border-bottom: 1px solid rgba(220, 38, 38, 0.3);
    padding-bottom: 10px;
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    gap: 10px;
}

.ippo-step-badge {
    font-family: 'Chakra Petch', sans-serif;
    font-size: 12px;
    background: var(--ippo-red);
    color: #fff;
    padding: 3px 10px;
    border-radius: 6px;
    letter-spacing: 0.5px;
}

/* ====== حقول الإدخال والبلوكات ====== */
.block, .form {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
}

label span, .block label {
    color: #e2e8f0 !important;
    font-weight: 700 !important;
    font-size: 14px !important;
    margin-bottom: 6px !important;
}

input[type="text"],
input[type="number"],
textarea {
    background: #07070c !important;
    color: #ffffff !important;
    border: 1px solid rgba(255, 255, 255, 0.14) !important;
    border-radius: 12px !important;
    padding: 12px 14px !important;
    transition: border-color 0.2s, box-shadow 0.2s !important;
}

input:focus, textarea:focus {
    border-color: var(--ippo-red) !important;
    box-shadow: 0 0 0 3px rgba(220, 38, 38, 0.22) !important;
    outline: none !important;
}

/* ====== القوائم المنسدلة سوداء بالكامل (Gradio 4 & 5) ====== */
.gradio-dropdown,
div[data-testid="dropdown"],
.wrap-inner,
.secondary-wrap {
    background: #07070c !important;
    color: #ffffff !important;
    border-radius: 12px !important;
}

.secondary-wrap {
    border: 1px solid rgba(255, 255, 255, 0.16) !important;
    padding: 4px 8px !important;
    transition: all 0.2s ease !important;
}

.secondary-wrap:hover,
.secondary-wrap:focus-within {
    border-color: var(--ippo-red) !important;
    box-shadow: 0 0 14px rgba(220, 38, 38, 0.28) !important;
}

.secondary-wrap input {
    background: #07070c !important;
    color: #ffffff !important;
    border: none !important;
    box-shadow: none !important;
}

/* القائمة المفتوحة */
ul.options,
.options {
    background: #050508 !important;
    border: 1px solid rgba(220, 38, 38, 0.45) !important;
    border-radius: 12px !important;
    box-shadow: 0 14px 35px rgba(0, 0, 0, 0.9) !important;
    z-index: 9999 !important;
}

ul.options li.item,
.options .item {
    background: #050508 !important;
    color: #f8fafc !important;
    padding: 11px 14px !important;
    font-weight: 600 !important;
    transition: background 0.15s ease !important;
}

ul.options li.item:hover,
ul.options li.item.active,
.options .item:hover,
.options .item.selected {
    background: rgba(220, 38, 38, 0.26) !important;
    color: #ffffff !important;
}

/* ====== الأزرار الكبيرة + توهج + حركة ضغط ====== */
button.primary, .ippo-btn-primary {
    background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%) !important;
    color: #ffffff !important;
    border: 1px solid rgba(245, 158, 11, 0.45) !important;
    border-radius: 14px !important;
    padding: 14px 26px !important;
    font-size: 16px !important;
    font-weight: 800 !important;
    cursor: pointer !important;
    box-shadow: 0 6px 20px rgba(220, 38, 38, 0.35) !important;
    transition: transform 0.15s ease, box-shadow 0.25s ease, filter 0.2s ease !important;
}

button.primary:hover, .ippo-btn-primary:hover {
    box-shadow: 0 0 26px rgba(220, 38, 38, 0.65), 0 0 10px rgba(245, 158, 11, 0.35) !important;
    filter: brightness(1.08) !important;
    transform: translateY(-1px) !important;
}

button.primary:active, .ippo-btn-primary:active {
    transform: scale(0.97) !important;
}

button.secondary, .ippo-btn-secondary {
    background: #111119 !important;
    color: #e2e8f0 !important;
    border: 1px solid rgba(255, 255, 255, 0.16) !important;
    border-radius: 14px !important;
    padding: 14px 22px !important;
    font-size: 15px !important;
    font-weight: 700 !important;
    transition: transform 0.15s ease, border-color 0.2s ease, box-shadow 0.2s ease !important;
}

button.secondary:hover, .ippo-btn-secondary:hover {
    border-color: var(--ippo-gold) !important;
    box-shadow: 0 0 16px var(--ippo-gold-glow) !important;
    color: #ffffff !important;
}

button.secondary:active, .ippo-btn-secondary:active {
    transform: scale(0.97) !important;
}

/* ====== صندوق اللوجات (Terminal) ====== */
.ippo-terminal {
    background: #050508 !important;
    border: 1px solid rgba(255, 255, 255, 0.12) !important;
    border-top: 3px solid var(--ippo-red) !important;
    border-radius: 14px !important;
    padding: 16px !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 13px !important;
    line-height: 1.75 !important;
    max-height: 290px !important;
    overflow-y: auto !important;
    direction: ltr !important;
    text-align: left !important;
    box-shadow: inset 0 0 25px rgba(0, 0, 0, 0.85) !important;
}

.ippo-log-ok   { color: #22c55e; font-weight: 600; }
.ippo-log-err  { color: #ef4444; font-weight: 600; }
.ippo-log-warn { color: #eab308; font-weight: 600; }
.ippo-log-info { color: #94a3b8; }

/* ====== التوافق مع الموبايل ====== */
@media (max-width: 768px) {
    .gradio-container > .main { padding: 10px !important; }
    .ippo-step-card { padding: 16px !important; }
    .ippo-rounds-row { gap: 5px; }
    .ippo-round-node { padding: 8px 6px; font-size: 12px; flex-direction: column; gap: 4px; }
    .ippo-nav-row { flex-direction: column-reverse !important; gap: 10px !important; }
    button.primary, button.secondary { width: 100% !important; }
}
"""


_CLEAR_FLOW_CSS = """
    .clear-flow {direction:rtl; width:100%!important;max-width:860px;margin:auto}
    .gradio-container h1,.gradio-container h2,.gradio-container h3,.gradio-container p,.clear-flow p,.clear-flow strong,.clear-flow span,.clear-flow label {color:#f8fafc!important}
    [role=tab]{color:#94a3b8!important;font-weight:800!important} [role=tab][aria-selected=true]{color:#f59e0b!important;border-color:#dc2626!important}
    .clear-flow input,.clear-flow textarea,.clear-flow select,.clear-flow [role=listbox],.clear-flow [role=option] {background:#07070c!important;color:#f8fafc!important}
    .clear-flow label {background:transparent!important}
    .clear-flow input[type=radio],.clear-flow input[type=checkbox] {accent-color:#dc2626!important;appearance:auto!important;opacity:1!important}
    .clear-flow input[type=radio]:checked+span{color:#f59e0b!important;font-weight:700}
    .episode-picker label {cursor:pointer!important;min-height:44px!important;display:flex!important;align-items:center!important;gap:10px!important;border:1px solid rgba(255,255,255,.09)!important;border-radius:10px!important;background:#0c0c12!important}
    .episode-picker input[type=checkbox] {appearance:none!important;-webkit-appearance:none!important;display:inline-grid!important;place-content:center!important;flex:0 0 22px!important;width:22px!important;height:22px!important;min-width:22px!important;opacity:1!important;border:2px solid #64748b!important;border-radius:5px!important;background:#07070c!important;position:relative!important;pointer-events:auto!important;margin:0!important}
    .episode-picker input[type=checkbox]:checked {background:#dc2626!important;border-color:#f59e0b!important}
    .episode-picker input[type=checkbox]:checked::after {content:"✓";color:white!important;font-size:18px!important;font-weight:900!important;line-height:18px!important}
    .episode-picker label:has(input:checked) {background:rgba(220,38,38,.18)!important;border:1px solid #f59e0b!important}
    .clear-flow .file-preview, .clear-flow .file-preview table, .clear-flow .file-preview tr, .clear-flow .file-preview td {background:#0c0c12!important;color:#f8fafc!important}
    .clear-flow .file-preview a {color:#f59e0b!important}
    .clear-flow input[type=radio]:not(:disabled), .clear-flow label:has(input[type=radio]:not(:disabled)), .clear-flow input[type=checkbox]:not(:disabled) {cursor:pointer!important;pointer-events:auto!important}
    .clear-flow button {font-size:16px!important}
    .clear-flow .prose {line-height:1.8}
    """



_IPPO_GRADIO_CSS = """
    .ippo-s2 .gr-group, .ippo-s2 .gradio-group, .ippo-s2 div.group, .ippo-s2 [class*="group"]:not(.ippo-s2):not(.arena-card):not([class*="gradio-container"]) {background:rgba(255,255,255,.035)!important; border:1px solid rgba(255,255,255,.1)!important; border-radius:14px!important; color:#e5e7eb!important; box-shadow:none!important}
    .ippo-s2 .gr-group *, .ippo-s2 .gradio-group *, .ippo-s2 div.group * {color:inherit}
    .ippo-s2 .prose, .ippo-s2 .prose *, .ippo-s2 label span, .ippo-s2 .md * {color:#e5e7eb!important}
    .ippo-s2 .prose h3 {color:#fff!important; font-weight:800}
    .ippo-s2 .styler, .ippo-s2 .gr-group .styler {background:transparent!important; --block-background-fill:transparent!important; --background-fill-primary:transparent!important; --background-fill-secondary:transparent!important}
    .ippo-s2 .gr-group {--block-background-fill:transparent!important; --body-text-color:#e5e7eb!important; --block-label-text-color:#cbd5e1!important; --block-title-text-color:#e5e7eb!important}
    .gradio-container .main, .gradio-container .contain {max-width:820px!important} .ippo-led, .ippo-led * {direction:rtl} .ippo-led strong {unicode-bidi:isolate} .ippo-led .prose, .ippo-led h1 {margin:0}
    .arena-card {position:relative; background:rgba(13,13,18,.94)!important; border:1px solid rgba(220,38,38,.32)!important; border-radius:18px!important; padding:22px!important; overflow:hidden!important}
    .arena-card::before {content:""; position:absolute; top:0; right:0; left:0; height:22px; pointer-events:none; z-index:20; background:linear-gradient(#ef4444,#ef4444) top right/22px 3px no-repeat, linear-gradient(#ef4444,#ef4444) top right/3px 22px no-repeat, linear-gradient(#f59e0b,#f59e0b) top left/22px 3px no-repeat, linear-gradient(#f59e0b,#f59e0b) top left/3px 22px no-repeat}
    .arena-card::after {content:""; position:absolute; bottom:0; right:0; left:0; height:22px; pointer-events:none; z-index:20; background:linear-gradient(#f59e0b,#f59e0b) bottom right/22px 3px no-repeat, linear-gradient(#f59e0b,#f59e0b) bottom right/3px 22px no-repeat, linear-gradient(#ef4444,#ef4444) bottom left/22px 3px no-repeat, linear-gradient(#ef4444,#ef4444) bottom left/3px 22px no-repeat}
    .ippo-title {display:flex; align-items:center; justify-content:space-between; gap:12px; padding-bottom:12px; margin-bottom:14px; border-bottom:1px solid rgba(255,255,255,.1)}
    .ippo-title .t {display:flex; align-items:center; gap:10px; font-size:22px; font-weight:900; color:#fff}
    .ippo-title .badge {font-family:'Oswald',sans-serif; font-size:12px; letter-spacing:.12em; color:#fbbf24; background:rgba(220,38,38,.14); border:1px solid rgba(220,38,38,.4); padding:4px 10px; border-radius:4px}
    .gradio-container input[type=text], .gradio-container input[type=password], .gradio-container input[type=number], .gradio-container textarea, .gradio-container select, .gradio-container [data-testid=dropdown] input {background:#050507!important; border:1.5px solid rgba(220,38,38,.3)!important; border-radius:12px!important; color:#fff!important}
    .gradio-container input:hover, .gradio-container textarea:hover {border-color:rgba(245,158,11,.68)!important}
    .gradio-container input:focus, .gradio-container textarea:focus {border-color:#ef4444!important; box-shadow:0 0 0 3px rgba(220,38,38,.24)!important}
    .gradio-container .block, .gradio-container .form, .gradio-container .gap {background:transparent!important}
    .gradio-container [data-testid=file-upload], .gradio-container .file-preview-holder, .gradio-container button.center, .gradio-container .upload-container {background:radial-gradient(circle at 50% 0%,rgba(220,38,38,.08),transparent 65%),#050507!important; border:2px dashed rgba(220,38,38,.45)!important; border-radius:14px!important}
    .gradio-container .block.padded, .gradio-container .gr-group, .gradio-container .group {border-radius:14px!important}
    .gradio-container button.primary {background:linear-gradient(135deg,#ef4444 0%,#dc2626 55%,#991b1b 100%)!important; color:#fff!important; border:1.5px solid rgba(251,191,36,.65)!important; border-radius:12px!important; box-shadow:0 6px 22px rgba(220,38,38,.42), inset 0 1px 0 rgba(254,240,138,.38)!important; font-weight:800!important}
    .gradio-container button.secondary {background:#050507!important; color:#e2e8f0!important; border:1.5px solid rgba(220,38,38,.3)!important; border-radius:12px!important}
    .gradio-container button.secondary:hover {border-color:#f59e0b!important}
    .gradio-container .accordion, .gradio-container .label-wrap {background:#09090e!important}
    #ippo-decor ~ * {position:relative}
    .ippo-confirm {background:rgba(220,38,38,.10)!important; border:1.5px solid #ef4444!important; border-radius:14px!important; padding:16px!important; margin:12px 0!important}
    .ippo-confirm > *, .ippo-confirm .block, .ippo-confirm .form, .ippo-confirm .row {background:transparent!important; border:none!important; box-shadow:none!important; padding:0!important}
    .ippo-confirm *, .ippo-confirm p, .ippo-confirm strong {color:#fff!important}
    .ippo-confirm .row {display:flex!important; gap:12px!important; align-items:stretch!important; margin-top:12px}
    .ippo-confirm .row > * {flex:1 1 0!important; min-width:0!important}
    .ippo-confirm button {height:48px!important; min-height:48px!important; width:100%!important; margin:0!important}
    .ippo-confirm button.secondary {background:#050507!important; border:1.5px solid rgba(220,38,38,.3)!important}
    .ippo-wipe, button.ippo-wipe {background:linear-gradient(135deg,#ef4444 0%,#dc2626 55%,#991b1b 100%)!important; color:#fff!important; border:1.5px solid rgba(251,191,36,.65)!important; border-radius:12px!important; box-shadow:0 6px 22px rgba(220,38,38,.42), inset 0 1px 0 rgba(254,240,138,.38)!important; font-weight:800!important; min-height:52px!important; transition:transform .14s ease, box-shadow .22s ease, filter .22s ease}
    .ippo-wipe:hover:not(:disabled) {box-shadow:0 0 26px rgba(239,68,68,.72), 0 0 16px rgba(245,158,11,.48)!important; filter:brightness(1.07)}
    .ippo-wipe:active:not(:disabled) {transform:scale(.97)}
    .ippo-wipe:disabled {opacity:.45!important; filter:grayscale(.4)}
    .sb-prog-card {background:#09090e; border:1px solid rgba(220,38,38,.32); border-radius:14px; padding:16px; color:#f8fafc; box-shadow:inset 0 3px 14px rgba(0,0,0,.9)}
    .sb-prog-top {display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px}
    .sb-prog-badge {font-family:'Oswald',sans-serif; font-size:12px; letter-spacing:.14em; color:#fbbf24; text-transform:uppercase}
    .sb-prog-title {font-size:17px; font-weight:800; color:#fff; margin-top:4px}
    .sb-prog-pct {font-family:'JetBrains Mono',monospace; font-size:20px; font-weight:700; color:#fbbf24}
    .sb-prog-track {position:relative; background:#050507; border:1.5px solid rgba(245,158,11,.42); border-radius:10px; height:22px; overflow:hidden; box-shadow:inset 0 3px 10px rgba(0,0,0,.95)}
    .sb-prog-fill {height:100%; background:repeating-linear-gradient(-45deg,rgba(255,255,255,.16) 0,rgba(255,255,255,.16) 8px,transparent 8px,transparent 16px), linear-gradient(90deg,#b91c1c 0%,#ef4444 48%,#f59e0b 86%,#fbbf24 100%); box-shadow:0 0 18px rgba(245,158,11,.72); transition:width .45s cubic-bezier(.22,1,.36,1)}
    .sb-mode-done .sb-prog-fill {background:repeating-linear-gradient(-45deg,rgba(255,255,255,.16) 0,rgba(255,255,255,.16) 8px,transparent 8px,transparent 16px), linear-gradient(90deg,#047857,#10b981); box-shadow:0 0 18px rgba(16,185,129,.6)}
    .sb-mode-error .sb-prog-fill {background:#991b1b; box-shadow:0 0 14px rgba(220,38,38,.6)}
    .sb-prog-meta {display:flex; flex-wrap:wrap; gap:8px 16px; margin-top:12px; font-family:'JetBrains Mono',monospace; font-size:12px; color:#cbd5e1}
    .sb-prog-meta span {color:#cbd5e1}
    .sb-grind {margin-top:14px} .sb-grind-head {display:flex; gap:12px; justify-content:space-between; margin-bottom:6px; font-size:12px; color:#cbd5e1}
    .sb-rank-live {color:#fbbf24; font-family:'Oswald',sans-serif; letter-spacing:.12em}

    .ippo-drop [data-testid=block-label], .ippo-drop label {display:none!important}
    .ippo-drop {background:transparent!important; border:none!important; box-shadow:none!important}
    .ippo-drop .upload-container {position:relative; display:flex!important; flex-direction:column!important; align-items:center!important; justify-content:flex-end!important; min-height:170px!important; height:auto!important; padding:70px 20px 22px!important; text-align:center; cursor:pointer;
      background:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2048%2048%22%20fill%3D%22none%22%3E%3Cpath%20d%3D%22M8%2032C8%2022%2015%2015%2024%2015C33%2015%2040%2022%2040%2032H8Z%22%20fill%3D%22%2318181b%22%20stroke%3D%22%23f59e0b%22%20stroke-width%3D%222%22/%3E%3Crect%20x%3D%226%22%20y%3D%2232%22%20width%3D%2236%22%20height%3D%225%22%20rx%3D%222.5%22%20fill%3D%22%23dc2626%22%20stroke%3D%22%23fbbf24%22%20stroke-width%3D%221.4%22/%3E%3Ccircle%20cx%3D%2224%22%20cy%3D%2211%22%20r%3D%223%22%20fill%3D%22%23fbbf24%22/%3E%3Cpath%20d%3D%22M18%2024H30M21%2020V28M27%2020V28%22%20stroke%3D%22%23ef4444%22%20stroke-width%3D%221.6%22%20stroke-linecap%3D%22round%22/%3E%3C/svg%3E") no-repeat 50% 22px/44px 44px,
        linear-gradient(#ef4444,#ef4444) top 8px right 8px/12px 2px no-repeat, linear-gradient(#ef4444,#ef4444) top 8px right 8px/2px 12px no-repeat,
        linear-gradient(#f59e0b,#f59e0b) top 8px left 8px/12px 2px no-repeat, linear-gradient(#f59e0b,#f59e0b) top 8px left 8px/2px 12px no-repeat,
        linear-gradient(#f59e0b,#f59e0b) bottom 8px right 8px/12px 2px no-repeat, linear-gradient(#f59e0b,#f59e0b) bottom 8px right 8px/2px 12px no-repeat,
        linear-gradient(#ef4444,#ef4444) bottom 8px left 8px/12px 2px no-repeat, linear-gradient(#ef4444,#ef4444) bottom 8px left 8px/2px 12px no-repeat,
        radial-gradient(circle at 50% 0%,rgba(220,38,38,.08),transparent 65%), #050507!important;
      border:2px dashed rgba(220,38,38,.45)!important; border-radius:14px!important; transition:all .24s ease}
    .ippo-drop .upload-container:hover {border-color:#f59e0b!important; box-shadow:0 0 24px rgba(220,38,38,.22), inset 0 0 16px rgba(245,158,11,.08)}
    .ippo-drop .upload-container [data-testid=upload-text] {display:none!important}
    .ippo-drop .upload-container::before {content:"اسحب الملف هنا، أو اضغط للاختيار"; font-weight:800; font-size:16px; color:#fff}
    .ippo-drop .upload-container::after {content:""; font-family:'JetBrains Mono',monospace; font-size:12px; color:#64748b; margin-top:4px}
    .ippo-drop-vid .upload-container::before {content:"اسحب ملف الفيديو أو التورنت هنا، أو اضغط للاختيار"}
    .ippo-drop-vid .upload-container::after {content:"MKV • MP4 • TORRENT"}
    .ippo-drop-tor .upload-container::before {content:"اسحب ملف التورنت هنا، أو اضغط للاختيار"}
    .ippo-drop-tor .upload-container::after {content:"TORRENT"}
    .ippo-drop-sub .upload-container::before {content:"اسحب ملف الترجمة هنا، أو اضغط للاختيار"}
    .ippo-drop-sub .upload-container::after {content:"ASS • SRT"}
"""


def _ippo_title(n, tag, text):
    return ('<div class="ippo-title"><div class="t"><span class="title-slash"></span>' + html.escape(text) +
            '</div><span class="badge">ROUND 0%d • %s</span></div>' % (n, tag))


def build_ui() -> gr.Blocks:
    """Independent tab instances. Only the current step is shown in each tab."""
    cfg = load_settings()
    if tgchan:
        try:
            tgchan.configure_drive(str(cfg.get("gdrive_remote", "gdrive")), str(cfg.get("gdrive_folder", "Runpod_Anime")))
            tgchan.restore_from_drive()
        except Exception:
            pass
    css = IPPO_CSS + THEME_CSS + _CLEAR_FLOW_CSS + _IPPO_GRADIO_CSS
    kwargs = _build_blocks_kwargs()
    if "css" in kwargs:
        kwargs["css"] = css
    if "head" in kwargs:
        kwargs["head"] = THEME_HEAD
    with gr.Blocks(**kwargs) as demo:
        gr.HTML("<style>" + css + "</style>")
        gr.HTML(HEADER_HTML)
        with gr.Column(elem_classes=["clear-flow"]):
            _build_clear_flow(cfg)
    return demo


def _wizard_source_text(source, uploaded_file):
    """Use one explicit source, never silently prefer links over an upload."""
    source = (source or "").strip()
    if uploaded_file:
        if source:
            raise ValueError("اختار مصدر واحد: امسح الروابط أو احذف الملف المرفوع.")
        path = Path(str(uploaded_file))
        if not path.is_file():
            raise ValueError("الملف المرفوع مش موجود. ارفعه تاني.")
        return str(path)
    return source



def _make_720_from_burned(files):
    """Single-episode: after the 1080p file is burned, build a 720p copy from it. No second burn or translation."""
    import shutil as _sh, subprocess as _sp
    ff = _sh.which("ffmpeg") or "ffmpeg"
    out = dict(files)
    for key, path in list(files.items()):
        try:
            src = Path(path)
            if not src.is_file():
                continue
            probe = _sp.run([_sh.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "csv=p=0", str(src)], capture_output=True, text=True)
            height = int((probe.stdout or "0").strip().split(",")[0] or 0)
            if height and height <= 720:
                continue
            name = src.name
            dst_name = name.replace("1080p", "720p") if "1080p" in name else src.stem + "_720p" + src.suffix
            dst = src.with_name(dst_name)
            ok = False
            for venc in (["-c:v", "hevc_nvenc", "-preset", "p6", "-cq", "27", "-pix_fmt", "p010le"], ["-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p"]):
                r = _sp.run([ff, "-y", "-i", str(src), "-vf", "scale=-2:720", *venc, "-c:a", "copy", "-movflags", "+faststart", str(dst)], capture_output=True, text=True)
                if r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
                    ok = True
                    break
            if ok:
                out[str(key) + " · 720p"] = str(dst)
        except Exception:
            continue
    return out


def _wipe_episode_files(items, job_state):
    """Delete only what this single-episode run created: its raw download folder and its output folder."""
    removed = []
    roots = [RAW_CACHE_ROOT.resolve(), STORAGE_ROOT.resolve()]
    cands = []
    for item in items or []:
        try:
            p = Path(item["path"]).resolve()
        except Exception:
            continue
        base = (RAW_CACHE_ROOT / "single").resolve()
        # raw download lives in RAW_CACHE_ROOT/single/<hash>/...
        if base in p.parents:
            cands.append(base / p.relative_to(base).parts[0])
        else:
            cands.append(p)  # a file inside the raw cache
    folder = (job_state or {}).get("folder")
    if folder:
        cands.append(Path(folder))
    for p in cands:
        try:
            p = p.resolve()
            if p in roots or not any(r in p.parents for r in roots) or not p.exists():
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed.append(str(p))
        except Exception:
            continue
    return removed


def _build_clear_flow(cfg):
    manifest = gr.State([])
    choices = gr.State({})
    file_map = gr.State({})
    job = gr.State({})
    upload_status = gr.State("")
    with gr.Column(elem_classes=["ippo-step-card","arena-card","ippo-s1"]) as source_group:
        gr.HTML(_ippo_title(1,"WEIGH-IN","مصدر الحلقة"))
        series_picker = gr.Dropdown(choices=[(SERIES_NEW_OPT, SERIES_NEW_OPT)] + list_saved_series(), value=SERIES_NEW_OPT, label="اختار أنمي موجود أو أنمي جديد", interactive=True)
        refresh_series = gr.Button("حدّث قائمة الأنمي", size="sm")
        series = gr.Textbox(label="اسم الأنمي الجديد", placeholder="مثال: Bleach", interactive=True, visible=(series_picker.value == SERIES_NEW_OPT))
        series_picker.change(lambda value: gr.update(visible=value == SERIES_NEW_OPT), [series_picker], [series], queue=False)
        refresh_series.click(lambda: gr.update(choices=[(SERIES_NEW_OPT, SERIES_NEW_OPT)] + list_saved_series()), outputs=[series_picker], queue=False)
        gr.Markdown("حط رابط واحد للحلقة. الأداة بتحرق نسخة ١٠٨٠ الأول وبعدين بتعمل منها نسخة ٧٢٠.")
        link_1080 = gr.Textbox(label="رابط الحلقة (Magnet / رابط مباشر / مسار ملف)", placeholder="magnet:?xt=... أو رابط مباشر أو مسار ملف")
        link_720 = gr.State("")
        source = gr.State("")
        local_file = gr.File(label="أو ارفع فيديو / ملف تورنت (بدل الرابط)", type="filepath", file_types=[".mp4", ".mkv", ".torrent", ".webm"], elem_classes=["ippo-drop","ippo-drop-vid"])
        picks = gr.State([])
        download = gr.Button("حمّل وتحقق من الحلقة", variant="primary")
        status = gr.Markdown("الترجمة مش هتبدأ في الخطوة دي.")
        next_button = gr.Button("التالي: الترجمة ←", interactive=False)
    with gr.Column(visible=False, elem_classes=["ippo-step-card","arena-card","ippo-s2"]) as subtitle_group:
        gr.HTML(_ippo_title(2,"STYLE","الترجمة والإخراج"))
        gr.Markdown("الحرق أساسي بجودة عالية والملف المحروق هو الناتج النهائي.")
        @gr.render(inputs=[manifest, choices])
        def render_rows(items, selections):
            for item in items or []:
                saved = (selections or {}).get(item["path"], {})
                arabic = None
                with gr.Group():
                    gr.Markdown("### " + html.escape(item["name"]))
                    mode = gr.Radio(["أوتوماتيك", "ترجمة يدوية"], value=saved.get("mode", "أوتوماتيك"), label="الترجمة", interactive=True, key="single-" + "mode-" + item["id"])
                    if saved.get("mode", "أوتوماتيك") == "أوتوماتيك":
                        try:
                            arabic = _wizard_arabic_track(item["path"])
                            if arabic:
                                gr.Markdown("✓ ترجمة عربية جاهزة: " + html.escape(_wizard_track_choices([arabic])[1][0]) + "<br>هنتخطى DeepSeek. نفس الصياغة والتوقيت، بستايلنا البصري فقط.")
                        except Exception:
                            gr.Markdown("تعذر فحص وجود ترجمة عربية. افحص الملف قبل التشغيل.")
                    if saved.get("mode") == "ترجمة يدوية" and saved.get("source_kind") != "mirai":
                        arabic = _wizard_arabic_track(item["path"])
                        if arabic:
                            gr.Markdown("✓ فيه ترجمة عربية جاهزة في الحلقة: " + html.escape(_wizard_track_choices([arabic])[1][0]))
                            use_arabic = gr.Button("استخدم العربي الموجود للحرق بدون AI", size="sm")
                            def choose_arabic(current, item=item, track=arabic):
                                original = _wizard_export_track(item["path"], track)
                                updated = _wizard_choice("ترجمة يدوية", original, current, item)[0]
                                updated[item["path"]]["source_kind"] = "embedded_arabic"
                                return updated
                        gr.Markdown("ملفك المرفوع SRT / ASS هيتحرق بمقاسات وخطوط وألوان ومكان ستايل Celestial Dragons، مع نفس كلامك وتوقيتك من غير تعديل.")
                        gr.Markdown("نزّل مسار الترجمة، ترجمه برا الأداة، وبعدها ارفع ملفك المترجم تحت. الحرق اليدوي بيستخدم ملفك المرفوع، أو العربي الموجود لو ضغطت زر استخدامه بنفسك.")
                        try:
                            tracks = _wizard_subtitle_tracks(item["path"])
                            if not tracks:
                                gr.Markdown("مفيش مسارات ترجمة داخل الحلقة. ارفع ملفك المترجم تحت.")
                            for track in tracks:
                                label = _wizard_track_choices([track])[1][0]
                                if track["supported"]:
                                    try:
                                        exported = _wizard_export_track(item["path"], track)
                                        gr.File(value=exported, label=label, interactive=False, height=80, key="single-"+item["id"]+"export-"+str(track["index"]))
                                    except Exception:
                                        gr.Markdown(html.escape(label) + " · تعذر استخراج المسار.")
                                else:
                                    gr.Markdown(html.escape(label))
                        except Exception as exc:
                            gr.Markdown(html.escape(str(exc)))
                    with gr.Accordion("ارفع ترجمة يدوية لنفس الحلقة · SRT / ASS", open=saved.get("mode") == "ترجمة يدوية" and saved.get("source_kind") != "mirai"):
                        upload = gr.File(label="ملف الترجمة اليدوية", type="filepath", file_types=[".srt", ".ass"], interactive=True, elem_classes=["ippo-drop","ippo-drop-sub"], key="single-" + "sub-" + item["id"])
                    if saved.get("mode") == "ترجمة يدوية" and arabic:
                        use_arabic.click(choose_arabic, [choices], [choices], concurrency_id="subtitle-choices", concurrency_limit=1)
                    if saved.get("subtitle"):
                        if saved.get("source_kind") == "mirai" and saved.get("mode") == "ترجمة يدوية":
                            gr.Markdown("✓ ترجمة جاهزة من miraianime (" + html.escape(saved.get("translator", "")) + "). بدون DeepSeek. اختار أوتوماتيك لو عايز API.")
                        else:
                            gr.Markdown("✓ العربي الموجود متحدد للحرق بدون AI" if saved.get("source_kind") == "embedded_arabic" else "✓ ملف الترجمة محفوظ لنفس الحلقة")
                    def change(mode_value, upload_value, current, item=item):
                        return _wizard_choice(mode_value, upload_value, current, item)
                    def save_choice(mode_value, upload_value, current, item=item):
                        if not upload_value:
                            upload_value = (current or {}).get(item["path"], {}).get("subtitle")
                        updated = _wizard_choice(mode_value, upload_value, current, item)[0]
                        return gr.skip() if updated == current else updated
                    for event in [mode.change, upload.upload]:
                        event(save_choice, [mode, upload, choices], [choices], concurrency_id="subtitle-choices", concurrency_limit=1)
        gr.Markdown("الحرق بجودة عالية (16) والملف المحروق هو الناتج النهائي — مفيش مرحلة ضغط منفصلة.")
        with gr.Accordion("إعدادات الترجمة والحرق", open=False):
            provider = gr.Dropdown(["DeepSeek API (موصى به)", "Gemini API", "OpenAI-Compatible / Local"], value=cfg["provider"], label="محرك الترجمة")
            key = gr.Textbox(value=cfg["api_key"], label="API Key", type="password")
            model = gr.Textbox(value=cfg["llm_model"], label="الموديل")
            base = gr.Textbox(value=cfg["base_url"], label="Base URL")
            codec = gr.Dropdown([("HEVC كرت الشاشة", "hevc_nvenc"), ("H.264 كرت الشاشة", "h264_nvenc"), ("HEVC معالج", "libx265"), ("AV1 كرت الشاشة", "av1_nvenc")], value=cfg["codec"], label="كوديك الإخراج")
            concurrency = gr.State(1)  # حلقة واحدة في المرة
            logo_position = gr.Dropdown([("أعلى اليمين","top-right"),("أعلى الشمال","top-left"),("أسفل اليمين","bottom-right"),("أسفل الشمال","bottom-left")],value=cfg.get("logo_position","top-right"),label="مكان اللوجو")
            rclone_conf_box = gr.Textbox(label="كونفج rclone (Google Drive): الصق الكتلة كلها هنا", lines=6, placeholder="[gdrive]\ntype = drive\nscope = drive\ntoken = {...}")
            rclone_btn = gr.Button("حفظ المفتاح وربط Drive")
            rclone_status = gr.Markdown("")
            rclone_btn.click(apply_rclone_config, [rclone_conf_box, key], [rclone_status], concurrency_limit=1)
        gr.Markdown("**الرفع التلقائي:** بعد ما كل الجودات تخلص، النتائج بتترفع لوحدها على الوجهات المتعلمة هنا (علّم على واحد أو أكتر):")
        with gr.Row():
            tg_upload = gr.Checkbox(value=bool(tgchan and tgchan.is_ready()),label="تيليجرام (قناتك الخاصة)")
            dood = gr.Checkbox(value=False,label="DoodStream")
            drive = gr.Checkbox(value=False,label="Google Drive")
        start = gr.Button("ابدأ الترجمة والحرق", variant="primary")
        back = gr.Button("رجوع للمصدر")
    with gr.Column(visible=False, elem_classes=["ippo-step-card","arena-card","ippo-s3"]) as processing_group:
        gr.HTML(_ippo_title(3,"PLAN","الترجمة ← الحرق ← الضغط ← الفحص"))
        progress = gr.HTML()
    with gr.Column(visible=False, elem_classes=["ippo-step-card","arena-card","ippo-s4"]) as result_group:
        gr.HTML(_ippo_title(4,"EVENT","النتائج والرفع"))
        preview = gr.Video(label="معاينة الناتج")
        videos = gr.Files(label="الفيديوهات الجاهزة")
        ass = gr.File(label="ملف الترجمة المنسق")
        en_files = gr.Files(label="ملفات الترجمة الإنجليزية")
        with gr.Accordion("ارفع النتائج", open=True):
            gr.Markdown("الرفع بيحصل **تلقائياً** بعد ما كل الجودات تخلص، على الوجهات اللي اخترتها في خطوة الترجمة قبل التشغيل. الزرار ده لإعادة الرفع اليدوي لو احتجت (تيليجرام بيتخطى اللي اترفع قبل كده).")
            dood_key = gr.Textbox(value=cfg.get("dood_api_key",""),type="password",label="DoodStream API Key")
            remote = gr.Textbox(value=cfg.get("gdrive_remote","gdrive"),label="Drive Remote")
            folder = gr.Textbox(value=cfg.get("gdrive_folder","Runpod_Anime"),label="فولدر Drive")
            upload_button = gr.Button("ارفع النتائج المختارة")
            links = gr.Textbox(label="روابط الرفع",lines=4)
        with gr.Accordion("📡 إعداد تيليجرام (مرة واحدة)", open=False):
            _tg0 = tgchan.get_ui_state() if tgchan else {"api_id": "", "api_hash": "", "phone": "", "channel_title": "", "enabled": False, "status_text": "❌ موديول تيليجرام مش متاح (telegram_channel.py أو telethon ناقصة)"}
            gr.Markdown("الرفع على قناة تيليجرام **خاصة بيك أنت بس**. الإعداد مرة واحدة: اكتب البيانات ← ابعت الكود ← سجّل الدخول ← أنشئ القناة. الجلسة بتتحفظ على Drive فمش هتسجل تاني حتى بعد ريستارت البود. الأرقام بتتجاب من my.telegram.org ← API development tools.")
            with gr.Row():
                tg_api_id = gr.Textbox(value=_tg0["api_id"], label="API ID (من my.telegram.org)")
                tg_api_hash = gr.Textbox(value=_tg0["api_hash"], label="API Hash", type="password")
                tg_phone = gr.Textbox(value=_tg0["phone"], label="رقم تيليجرام بالصيغة الدولية (+20...)")
            with gr.Row():
                tg_send_code_btn = gr.Button("1️⃣ إرسال كود الدخول", variant="secondary")
                tg_code = gr.Textbox(label="2️⃣ الكود اللي وصلك على تيليجرام")
                tg_password = gr.Textbox(label="باسورد التحقق بخطوتين (لو موجود)", type="password")
                tg_login_btn = gr.Button("3️⃣ تسجيل الدخول", variant="secondary")
            with gr.Row():
                tg_channel_title = gr.Textbox(value=_tg0["channel_title"], label="اسم القناة الخاصة")
                tg_setup_btn = gr.Button("📺 إنشاء / فحص القناة", variant="secondary")
            tg_status = gr.Textbox(value=_tg0["status_text"], label="حالة تيليجرام", lines=4, interactive=False)
        with gr.Group(visible=False, elem_classes=["ippo-confirm"]) as wipe_confirm:
            wipe_msg = gr.Markdown("")
            with gr.Row():
                wipe_yes = gr.Button("أيوة، امسح نهائياً", variant="stop", elem_classes=["ippo-wipe"])
                wipe_no = gr.Button("إلغاء", variant="secondary")
        again = gr.Button("امسح كل حاجة وارجع للبداية", variant="stop", interactive=False, elem_classes=["ippo-wipe"])
    with gr.Accordion("تفاصيل السجل عند الحاجة", open=False):
        logs = gr.Textbox(label="السجل",lines=10)
    hidden_local = gr.State(None)
    hidden_raw = gr.State(None)
    hidden_archive = gr.State(None)
    hidden_archive_text = gr.State(None)
    def download_source(src, link1080, link720, selected, mapping, anime, file_value, selected_series, pos=0):
        anime = anime if selected_series == SERIES_NEW_OPT else selected_series
        # وضع الحلقة الواحدة: رابط 1080p و/أو 720p لنفس الحلقة (أو ملف مرفوع بدلهم).
        # الجودتين بياخدوا نفس رقم الحلقة، والترجمة المكررة بتتعاد من ذاكرة الترجمة.
        try:
            if not anime.strip():
                raise ValueError("اكتب اسم الأنمي الأول.")
            link1080, link720 = (link1080 or "").strip(), (link720 or "").strip()
            if file_value and (link1080 or link720):
                raise ValueError("اختار مصدر واحد: الروابط أو الملف المرفوع، مش الاتنين مع بعض.")
            targets: List[Tuple[str, str, Any]] = []  # (جودة، رابط، ملف محلي)
            if file_value:
                targets.append(("", "", file_value))
            else:
                if link1080:
                    targets.append(("1080p", link1080, None))
                if link720:
                    targets.append(("720p", link720, None))
                if not targets:
                    raise ValueError("حط رابط الحلقة أو ارفع ملف.")
            pair_mode = len(targets) > 1
            merged: List[Dict[str, Any]] = []
            ep_label_shared: Optional[str] = None
            for q_label, lnk, fl in targets:
                local_vid = fl if fl else (lnk if lnk and Path(lnk).is_file() and Path(lnk).suffix.lower() in VIDEO_EXTENSIONS else None)
                cache_key = f"{q_label}|{lnk or fl}"
                path = download_torrent_or_url(lnk or None, local_vid,
                    str(RAW_CACHE_ROOT / "single" / hashlib.sha256(cache_key.encode()).hexdigest()[:20]))
                last_tagged: List[Dict[str, Any]] = []
                for values in step_download_wizard(str(path), [], {}, anime, SERIES_NEW_OPT):
                    last_tagged = []
                    for item in values[0]:
                        item = dict(item)
                        item["flow"] = "single"
                        if q_label:
                            item["quality"] = q_label
                            item["name"] = f"{q_label} · {item['name']}"
                        last_tagged.append(item)
                    yield merged + last_tagged, values[1], values[2], values[3], values[4]
                merged = merged + last_tagged
            if not merged:
                raise ValueError("مفيش حلقة اتأكدت من الروابط.")
            # رقم الحلقة الحقيقي من اسم الملف (أول نسخة) عشان التسمية وإعلان تيليجرام يطلعوا صح
            first_path = Path(merged[0]["path"])
            ep_num = extract_episode_number(first_path.name)
            ep_label_shared = f"EP{ep_num:03d}" if ep_num else "EP001"
            for item in merged:
                item["ep_label"] = ep_label_shared
            merged.sort(key=lambda item: 0 if item.get("quality", "1080p") == "1080p" else 1)
            qualities = " + ".join(item.get("quality") or "ملف مرفوع" for item in merged)
            yield merged, {}, gr.update(interactive=True), f"✓ اتأكدنا من الحلقة ({qualities}). اضغط التالي لملفات الترجمة.", gr.update(interactive=True)
        except Exception as exc:
            yield [], {}, gr.update(interactive=False), "❌ " + str(exc), gr.update(interactive=True)
        return
    download.click(download_source,[source,link_1080,link_720,picks,file_map,series,local_file,series_picker],[manifest,choices,next_button,status,download])
    def next_step(items):
        return _wizard_next(items)
    next_button.click(next_step,[manifest],[source_group,subtitle_group])
    back.click(lambda:(gr.update(visible=True),gr.update(visible=False)),outputs=[source_group,subtitle_group])
    def invalidate():
        return [],{},gr.update(interactive=False)
    link_1080.change(invalidate,outputs=[manifest,choices,next_button])
    series_picker.change(invalidate,outputs=[manifest,choices,next_button],queue=False)
    series.change(invalidate,outputs=[manifest,choices,next_button],queue=False)
    local_file.change(invalidate,outputs=[manifest,choices,next_button])
    def run(items, selection, anime, provider_v, key_v, model_v, base_v, codec_v, count_v, logo_v, selected_series):
        anime = anime if selected_series == SERIES_NEW_OPT else selected_series
        if not items:
            raise gr.Error("حمّل الملفات الأول.")
        source_paths = "\n".join(item["path"] for item in items)
        args = [source_paths,anime,SERIES_NEW_OPT,"bleach_style",cfg.get("wrap_mode","sequential"),cfg.get("soften_exclaim",True),
                "Auto (سحب ترجمة MKV إن وجدت، وإلا Whisper)","large-v3",provider_v,key_v,model_v,base_v,
                codec_v,cfg.get("preset","p6"),cfg.get("x265_preset","slow"),"copy",
                cfg.get("credit_translator",""),cfg.get("credit_typesetter",""),cfg.get("credit_encoder",""),False,
                logo_v,cfg.get("logo_opacity",0.3),cfg.get("logo_scale",0.12),False,count_v,"grid","",items,selection]
        completed = False
        last_job = {}
        for result in step_process_episodes(*args):
            result = list(result)
            failed = isinstance(result[8],dict) and result[8].get("visible") is True
            completed = completed or (isinstance(result[9],dict) and result[9].get("visible") is True)
            complete = completed
            if isinstance(result[7], dict) and result[7].get("files"):
                last_job = result[7]
            yield (result[0],result[1],result[2],result[5],result[6],result[7],result[14],
                   gr.update(visible=failed),gr.update(visible=not failed and not complete),gr.update(visible=complete))
        if completed and last_job.get("files"):
            new_files = _make_720_from_burned(last_job["files"])
            if len(new_files) != len(last_job["files"]):
                new_job = dict(last_job); new_job["files"] = new_files
                yield (gr.skip(),gr.skip(),gr.skip(),list(new_files.values()),gr.skip(),new_job,gr.skip(),gr.skip(),gr.skip(),gr.skip())
    def auto_upload(job_state, tg_v, dood_v, dood_key_v, drive_v, remote_v, folder_v, anime, pick):
        # رفع تلقائي بعد ما المعالجة تخلص — بس لو فيه نتائج ووجهة متعلمة من خطوة الترجمة
        if not job_state or not job_state.get("files"):
            yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), "failed"
            return
        if not (tg_v or dood_v or drive_v):
            yield gr.skip(), gr.skip(), "ℹ️ مفيش وجهة رفع متعلمة — النتائج محفوظة محلياً. تقدر ترفعها يدوياً من قسم «ارفع النتائج».", gr.skip(), "none"
            return
        yield gr.skip(), gr.skip(), gr.skip(), gr.update(visible=True), "pending"
        box = {}
        try:
            for hud, log_txt, links_txt in step2_approve_and_upload(job_state, dood_v, dood_key_v, drive_v, remote_v, folder_v, tg_v, anime, pick, status_box=box):
                yield hud, log_txt, links_txt, gr.skip(), gr.skip()
        finally:
            pass
        yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), box.get("state", "failed") if box.get("state") != "pending" else "failed"
    def manual_upload(job_state, dood_v, dood_key_v, drive_v, remote_v, folder_v, tg_v, anime, pick):
        box = {}
        for hud, log_txt, links_txt in step2_approve_and_upload(job_state, dood_v, dood_key_v, drive_v, remote_v, folder_v, tg_v, anime, pick, status_box=box):
            yield hud, log_txt, links_txt, gr.skip()
        yield gr.skip(), gr.skip(), gr.skip(), box.get("state", "failed") if box.get("state") != "pending" else "failed"
    start_event = start.click(run,[manifest,choices,series,provider,key,model,base,codec,concurrency,logo_position,series_picker],
                [progress,logs,preview,videos,ass,job,en_files,subtitle_group,processing_group,result_group])
    upload_event = start_event.then(auto_upload,[job,tg_upload,dood,dood_key,drive,remote,folder,series,series_picker],[progress,logs,links,processing_group,upload_status])
    if tgchan:
        tg_send_code_btn.click(fn=tgchan.ui_send_code, inputs=[tg_api_id, tg_api_hash, tg_phone], outputs=[tg_status])
        tg_login_btn.click(fn=tgchan.ui_complete_login, inputs=[tg_code, tg_password], outputs=[tg_status])
        tg_setup_btn.click(fn=tgchan.ui_ensure_channel, inputs=[tg_channel_title], outputs=[tg_status])
        tg_setup_btn.click(fn=lambda: gr.update(value=bool(tgchan.is_ready())), outputs=[tg_upload], queue=False)
    upload_button.click(lambda: gr.update(visible=True),outputs=[processing_group],queue=False)
    upload_event2 = upload_button.click(manual_upload,[job,dood,dood_key,drive,remote,folder,tg_upload,series,series_picker],[progress,logs,links,upload_status])
    wipe_outputs = [manifest, choices, file_map, job, link_1080, local_file, series, series_picker, status,
                    next_button, download, progress, logs, preview, videos, ass, en_files, links, again,
                    source_group, subtitle_group, processing_group, result_group, wipe_confirm, upload_status]
    def _reset_values():
        return ([], {}, {}, {}, "", None, "", gr.update(value=SERIES_NEW_OPT), "الترجمة مش هتبدأ في الخطوة دي.",
                gr.update(interactive=False), gr.update(interactive=True), "", "", None, None, None, None, "",
                gr.update(interactive=False),
                gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
                gr.update(visible=False), "")
    def do_wipe(items, job_state):
        _wipe_episode_files(items, job_state)
        return _reset_values()
    def request_wipe(items, job_state, state):
        if state == "ok":
            return do_wipe(items, job_state)
        skip = [gr.skip()] * len(wipe_outputs)
        skip[wipe_outputs.index(wipe_confirm)] = gr.update(visible=True)
        return tuple(skip)
    again.click(request_wipe, [manifest, job, upload_status], wipe_outputs)
    again.click(lambda s: ("**ما اترفعتش أي حاجة.** الملف المحلي هو النسخة الوحيدة. لو مسحت دلوقتي هتضيع نهائياً." if s == "none" else "**الرفع فشل أو ما خلصش.** الملف المحلي ممكن يكون النسخة الوحيدة. لو مسحت دلوقتي هتضيع نهائياً."), [upload_status], [wipe_msg], queue=False)
    wipe_yes.click(do_wipe, [manifest, job], wipe_outputs)
    wipe_no.click(lambda: gr.update(visible=False), outputs=[wipe_confirm], queue=False)
    start.click(lambda: (gr.update(interactive=False), gr.update(visible=False)), outputs=[again, wipe_confirm], queue=False)
    upload_event.then(lambda: gr.update(interactive=True), outputs=[again], queue=False)
    upload_event2.then(lambda: gr.update(interactive=True), outputs=[again], queue=False)
if __name__ == "__main__":
    ui = build_ui()
    user, pwd = os.environ.get("UI_USER", "").strip(), os.environ.get("UI_PASS", "").strip()
    if not (user and pwd) and os.environ.get("ALLOW_NO_AUTH") != "1":
        raise SystemExit("لازم UI_USER و UI_PASS (باسورد للواجهة) عشان مفاتيحك متتشافش. شغّل الأداة بـ run.sh وهو بيعمله لوحده.")

    launch_sig = inspect.signature(ui.launch).parameters
    launch_kwargs = {
        "server_name": "0.0.0.0",
        "server_port": int(os.environ.get("PORT", "7860")),
        "share": os.environ.get("GRADIO_SHARE", "false").lower() == "true",
        "auth": (user, pwd) if (user and pwd) else None,
    }
    if "css" in launch_sig:
        launch_kwargs["css"] = IPPO_CSS + THEME_CSS + _CLEAR_FLOW_CSS + _IPPO_GRADIO_CSS
    if "head" in launch_sig:
        launch_kwargs["head"] = THEME_HEAD
    if "allowed_paths" in launch_sig:
        launch_kwargs["allowed_paths"] = [str(STORAGE_ROOT), str(RAW_CACHE_ROOT)]
    if "delete_cache" in launch_sig:
        launch_kwargs["delete_cache"] = (86400, 86400)

    from export_page import start_export_server
    try:
        export_ui = start_export_server(auth=launch_kwargs["auth"])
    except Exception as exc:
        print("Export page did not start: " + str(exc), flush=True)
    ui.queue().launch(**launch_kwargs)
