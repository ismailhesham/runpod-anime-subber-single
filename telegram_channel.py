"""رفع الحلقات على قناة تيليجرام خاصة — runpod-anime-subber.

يستخدم Telethon (حساب المستخدم نفسه) عشان حد البوت العادي 50 ميجا، أما الحساب
العادي بيرفع لحد ~2 جيجا للملف. القناة بتتعمل برايفت على حساب المستخدم، وهو العضو
الوحيد فيها. تسجيل الدخول مرة واحدة، والجلسة بتتحفظ بره فولدر الكود وبتتنسخ على
Google Drive عشان تنجو من مسح البود.

مفيش أي سر بيتكتب في الكود أو الريبو: api_id / api_hash / الكود / الباسورد بيدخلهم
المستخدم في واجهة الأداة وبيتحفظوا في /workspace/telegram_state.json (بره الريبو).
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    from telethon import TelegramClient, functions, types
    from telethon.errors import (
        ApiIdInvalidError,
        FloodWaitError,
        PasswordHashInvalidError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )
    TG_LIB_OK = True
except Exception:  # telethon مش متسطبة لسه (pip install -r requirements.txt)
    TG_LIB_OK = False

STATE_FILE = Path(os.environ.get("RUNPOD_TG_STATE", "/workspace/telegram_state.json"))
SESSION_FILE = str(os.environ.get("RUNPOD_TG_SESSION", "/workspace/telegram_session"))

DEFAULT_CHANNEL_TITLE = "⚔️ حلقات الأنمي | Animerza"
CHANNEL_ABOUT = "أرشيف حلقات الأنمي المترجمة — قناة خاصة"
BRAND_FOOTER = "⚔️ Animerza"
UPLOAD_GAP_SECONDS = 20  # فاصل هادي بين رفع حلقة والتانية عشان تيليجرام ما يقيّدش الحساب
DEDUPE_KEEP = 300

_drv_remote: str = ""
_drv_folder: str = ""

LogCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[float, str, str], None]]


def _log(cb: LogCallback, msg: str) -> None:
    if callable(cb):
        try:
            cb(msg)
        except Exception:
            pass


# ---------------------------------------------------------------- state ----
def _load_state() -> Dict[str, Any]:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(st: Dict[str, Any], backup: bool = True) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(STATE_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    if backup:
        _drive_push(STATE_FILE.name)


# ------------------------------------------------------------ drive sync ----
def configure_drive(remote: str, folder: str) -> None:
    """بتتنده مرة من app.py بعد تحميل الإعدادات."""
    global _drv_remote, _drv_folder
    _drv_remote = (remote or "").strip()
    _drv_folder = (folder or "").strip()


def _rclone_ok() -> bool:
    if not _drv_remote:
        return False
    try:
        out = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=30)
        return (_drv_remote + ":") in out.stdout
    except Exception:
        return False


def _drive_push(filename: str) -> None:
    src = STATE_FILE.parent / filename
    if not src.is_file() or not _rclone_ok():
        return
    try:
        subprocess.run(
            ["rclone", "copyto", str(src), f"{_drv_remote}:{_drv_folder}/_state/telegram/{filename}"],
            capture_output=True, timeout=300,
        )
    except Exception:
        pass


def restore_from_drive(log_cb: LogCallback = None) -> None:
    """بود جديد: نزّل الجلسة والحالة من Drive لو مش موجودين محلياً."""
    if not _rclone_ok():
        return
    sess = Path(SESSION_FILE + ".session")
    targets = [(STATE_FILE, STATE_FILE.name), (sess, sess.name)]
    for dest, name in targets:
        if dest.is_file():
            continue
        try:
            res = subprocess.run(
                ["rclone", "copyto", f"{_drv_remote}:{_drv_folder}/_state/telegram/{name}", str(dest)],
                capture_output=True, timeout=300,
            )
            if res.returncode == 0 and dest.is_file():
                _log(log_cb, f"♻️ اتسترجع {name} من Drive — تسجيل دخول تيليجرام لسه شغال.")
        except Exception:
            pass


def backup_session() -> None:
    try:
        os.chmod(SESSION_FILE + ".session", 0o600)
    except Exception:
        pass
    _drive_push(Path(SESSION_FILE + ".session").name)


# ------------------------------------------------------------ tg runner ----
class _Runner:
    """خيط واحد شغال عليه asyncio loop بتاع Telethon — عشان Gradio بيهندل بخيوط عادية."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self._main, daemon=True, name="tg-loop")
        self._t.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: Optional[float] = 120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


_runner: Optional[_Runner] = None
_client = None
_pending_phone: str = ""
_pending_hash: str = ""
_ready_cache: Dict[str, Any] = {"ok": False, "at": 0.0}


def _get_runner() -> _Runner:
    global _runner
    if _runner is None:
        _runner = _Runner()
    return _runner


async def _ensure_client():
    global _client
    if _client is not None:
        return _client
    st = _load_state()
    try:
        api_id = int(str(st.get("api_id") or "0").strip())
    except ValueError:
        api_id = 0
    api_hash = str(st.get("api_hash") or "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("مفيش API ID / API Hash محفوظين — اكتبهم في إعدادات تيليجرام الأول.")
    client = TelegramClient(SESSION_FILE, api_id, api_hash, loop=_get_runner().loop)
    await client.connect()
    _client = client
    return client


def _fmt_me(me) -> str:
    if me is None:
        return "حسابك"
    name = " ".join(p for p in [getattr(me, "first_name", "") or "", getattr(me, "last_name", "") or ""]).strip()
    uname = getattr(me, "username", "") or ""
    if uname:
        return f"{name} (@{uname})".strip()
    return name or "حسابك"


# ---------------------------------------------------------------- login ----
def ui_send_code(api_id: str, api_hash: str, phone: str) -> str:
    if not TG_LIB_OK:
        return "❌ مكتبة telethon مش متسطبة على البود. شغّل: pip install -r requirements.txt وبعدين افتح الأداة من الأول."
    api_id_s = str(api_id or "").strip()
    api_hash_s = str(api_hash or "").strip()
    phone_s = str(phone or "").strip().replace(" ", "")
    if not api_id_s or not api_hash_s or not phone_s:
        return "⚠️ اكتب API ID و API Hash ورقم التليفون (بكود الدولة، زي +2010...) وبعدين دوس إرسال الكود."
    try:
        api_id_i = int(api_id_s)
    except ValueError:
        return "⚠️ API ID لازم يكون أرقام بس (من my.telegram.org)."
    st = _load_state()
    st.update({"api_id": api_id_i, "api_hash": api_hash_s, "phone": phone_s})
    _save_state(st)

    async def _go() -> str:
        global _client, _pending_hash, _pending_phone
        if _client is not None:
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None
        c = await _ensure_client()
        if await c.is_user_authorized():
            me = await c.get_me()
            return f"✅ أنت مسجل دخول أصلاً باسم: {_fmt_me(me)} — مش محتاج كود. دوس «إنشاء / فحص القناة» وكمّل."
        sent = await c.send_code_request(phone_s)
        _pending_hash = sent.phone_code_hash
        _pending_phone = phone_s
        return "📨 الكود اتبعت على تيليجرام (هتلاقيه في الشات المحفوظ/رسائل تيليجرام الرسمية). اكتبه في خانة الكود ودوس «تسجيل الدخول»."

    try:
        return _get_runner().run(_go(), timeout=90)
    except FloodWaitError as e:
        return f"⏳ تيليجرام طلب انتظار {e.seconds} ثانية قبل طلب كود تاني (محاولات كتير). استنى وجرب."
    except PhoneNumberInvalidError:
        return "⚠️ رقم التليفون غلط. اكتبه بالصيغة الدولية زي +201012345678."
    except ApiIdInvalidError:
        return "⚠️ API ID / API Hash غلط. اتأكد إنك ناسخهم صح من my.telegram.org."
    except Exception as e:
        return f"❌ خطأ أثناء إرسال الكود: {e}"


def ui_complete_login(code: str, password: str) -> str:
    if not TG_LIB_OK:
        return "❌ مكتبة telethon مش متسطبة على البود."
    code_s = str(code or "").strip().replace("-", "").replace(" ", "")
    pwd_s = str(password or "")

    async def _go() -> str:
        c = await _ensure_client()
        if await c.is_user_authorized():
            me = await c.get_me()
            return f"✅ تمام — مسجل دخول باسم: {_fmt_me(me)}. دوس «إنشاء / فحص القناة» وفعّل الرفع."
        if not _pending_phone and not pwd_s:
            return "⚠️ دوس «إرسال كود الدخول» الأول، وبعدين اكتب الكود هنا."
        try:
            if code_s:
                await c.sign_in(phone=_pending_phone, code=code_s, phone_code_hash=_pending_hash or None)
            elif pwd_s:
                await c.sign_in(password=pwd_s)
            else:
                return "⚠️ اكتب الكود اللي وصلك على تيليجرام."
        except SessionPasswordNeededError:
            if not pwd_s:
                return "🔐 حسابك عليه تحقق بخطوتين: اكتب باسورد تيليجرام في خانة الباسورد ودوس «تسجيل الدخول» تاني."
            await c.sign_in(password=pwd_s)
        me = await c.get_me()
        st = _load_state()
        st["logged_in_as"] = _fmt_me(me)
        _save_state(st)
        backup_session()
        _ready_cache.update({"ok": True, "at": time.time()})
        return f"✅ اتسجل الدخول بنجاح باسم: {_fmt_me(me)}\nالجلسة اتحفظت واتنسخت على Drive — مش هتسجل تاني حتى لو البود اتقفل.\nدلوقتي دوس «إنشاء / فحص القناة»."

    try:
        return _get_runner().run(_go(), timeout=90)
    except PhoneCodeInvalidError:
        return "⚠️ الكود غلط. بص على تيليجرام واكتبه صح."
    except PhoneCodeExpiredError:
        return "⚠️ الكود خلصت صلاحيته. دوس «إرسال كود الدخول» من الأول."
    except PasswordHashInvalidError:
        return "⚠️ باسورد التحقق بخطوتين غلط. جرب تاني."
    except FloodWaitError as e:
        return f"⏳ تيليجرام طلب انتظار {e.seconds} ثانية. استنى وجرب."
    except Exception as e:
        return f"❌ خطأ أثناء تسجيل الدخول: {e}"


# -------------------------------------------------------------- channel ----
def ui_ensure_channel(title: str) -> str:
    if not TG_LIB_OK:
        return "❌ مكتبة telethon مش متسطبة على البود."
    title_s = (title or "").strip() or DEFAULT_CHANNEL_TITLE

    async def _go() -> str:
        c = await _ensure_client()
        if not await c.is_user_authorized():
            return "⚠️ سجّل الدخول الأول (إرسال الكود ← تسجيل الدخول) وبعدين ارجع هنا."
        st = _load_state()
        ent = None
        if st.get("channel_id"):
            try:
                ent = await c.get_entity(types.PeerChannel(int(st["channel_id"])))
            except Exception:
                ent = None
        if ent is None:
            res = await c(functions.channels.CreateChannelRequest(
                title=title_s, about=CHANNEL_ABOUT, broadcast=True,
            ))
            ent = res.chats[0]
            st["channel_id"] = int(ent.id)
        else:
            try:
                await c(functions.channels.EditTitleRequest(channel=ent, title=title_s))
            except Exception:
                pass
        invite = st.get("invite") or ""
        try:
            inv = await c(functions.messages.ExportChatInviteRequest(peer=ent))
            invite = inv.link
        except Exception:
            pass
        st.update({"channel_title": title_s, "invite": invite})
        _save_state(st)
        backup_session()
        return (
            f"✅ القناة الخاصة جاهزة: {title_s}\n"
            f"🔗 لينك الدخول (لك أنت بس): {invite or 'افتحها من تيليجرام عندك'}\n"
            "فعّل اختيار «ارفع كل حلقة على تيليجرام» وكل حلقة جاية هتترفع لوحدها."
        )

    try:
        return _get_runner().run(_go(), timeout=120)
    except FloodWaitError as e:
        return f"⏳ تيليجرام طلب انتظار {e.seconds} ثانية. استنى وجرب."
    except Exception as e:
        return f"❌ خطأ أثناء تجهيز القناة: {e}"


def ui_set_enabled(flag: bool) -> str:
    st = _load_state()
    st["enabled"] = bool(flag)
    _save_state(st, backup=False)
    if flag:
        if not is_ready():
            return "⚠️ الرفع اتفعّل، بس تيليجرام لسه مش جاهز: كمّل تسجيل الدخول وإنشاء القناة من فوق."
        return "✅ الرفع على تيليجرام شغال — كل حلقة هتترفع على قناتك الخاصة أول بأول."
    return "⏸️ الرفع على تيليجرام متوقف — الحلقات هتتحفظ محلي وعلى Drive بس."


def is_ready() -> bool:
    if not TG_LIB_OK:
        return False
    if time.time() - _ready_cache["at"] < 60 and _ready_cache["ok"]:
        return True
    st = _load_state()
    if not st.get("channel_id"):
        return False

    async def _go() -> bool:
        c = await _ensure_client()
        return bool(await c.is_user_authorized())

    try:
        ok = bool(_get_runner().run(_go(), timeout=30))
    except Exception:
        ok = False
    _ready_cache.update({"ok": ok, "at": time.time()})
    return ok


def get_ui_state() -> Dict[str, Any]:
    st = _load_state()
    logged = ""
    if is_ready():
        logged = str(st.get("logged_in_as") or "مسجل الدخول")
    lines = []
    if not TG_LIB_OK:
        lines.append("❌ telethon مش متسطبة — نفّذ: pip install -r requirements.txt")
    elif logged:
        lines.append(f"✅ مسجل الدخول: {logged}")
    else:
        lines.append("⏳ لسه مش مسجل دخول — نفّذ خطوات تسجيل الدخول من فوق (إرسال الكود ← تسجيل الدخول).")
    if st.get("channel_title"):
        lines.append(f"📺 القناة: {st.get('channel_title')}")
        if st.get("invite"):
            lines.append(f"🔗 {st['invite']}")
    else:
        lines.append("📺 القناة لسه ما اتعملتش — هتتعمل أول ما تدوس «إنشاء / فحص القناة» أو مع أول رفع.")
    lines.append("الحالة: " + ("🟢 الرفع شغال" if st.get("enabled") else "⚪ الرفع واقف (فعّله من الاختيار فوق)"))
    return {
        "api_id": str(st.get("api_id") or ""),
        "api_hash": str(st.get("api_hash") or ""),
        "phone": str(st.get("phone") or ""),
        "channel_title": str(st.get("channel_title") or DEFAULT_CHANNEL_TITLE),
        "enabled": bool(st.get("enabled")),
        "status_text": "\n".join(lines),
    }


# --------------------------------------------------------------- upload ----
def _probe_video(path: str) -> Dict[str, int]:
    """مدة وأبعاد الفيديو بـ ffprobe عشان تيليجرام يعرضه كفيديو قابل للتشغيل."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
        dur = int(float(data.get("format", {}).get("duration", 0) or 0))
        w = h = 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = int(s.get("width", 0) or 0), int(s.get("height", 0) or 0)
                break
        return {"duration": dur, "w": w, "h": h}
    except Exception:
        return {"duration": 0, "w": 0, "h": 0}


def build_caption(series_name: str, ep_num: int, quality: str = "") -> str:
    series = (series_name or "").strip() or "Anime"
    q = f" | {quality}" if quality else ""
    if ep_num and ep_num > 0:
        return f"الحلقة {ep_num} من {series}{q}\n{series} | EP{ep_num:03d}{q}\n{BRAND_FOOTER}"
    return f"{series}{q}\n{BRAND_FOOTER}"


def upload_episode(
    series_name: str,
    ep_num: int,
    mp4_path: str,
    log_cb: LogCallback = None,
    progress_cb: ProgressCallback = None,
) -> str:
    """يرفع حلقة واحدة على القناة الخاصة. بينده من بايب لاين الأداة (خيوط Gradio)."""
    if not TG_LIB_OK:
        raise RuntimeError("telethon مش متسطبة على البود.")
    p = Path(mp4_path)
    if not p.is_file():
        raise RuntimeError(f"الملف مش موجود: {mp4_path}")
    size_mb = p.stat().st_size / (1024 * 1024)
    if size_mb > 1950:
        raise RuntimeError(f"الملف {size_mb:.0f} ميجا — أكبر من حد تيليجرام للحسابات العادية (~2 جيجا).")

    st = _load_state()
    series = (series_name or "").strip() or "Anime"
    meta = _probe_video(str(p))
    quality = f"{meta['h']}p" if meta.get("h") else ""
    # الجودة جزء من مفتاح منع التكرار: نفس الحلقة بجودتين (1080p و720p) بتترفع عادي،
    # لكن نفس الجودة لنفس الحلقة ما تترفعش مرتين.
    ep_key = f"{series}|EP{int(ep_num):03d}|{quality or p.stem}" if ep_num else f"{series}|{p.name}"
    done = set(st.get("uploads") or [])
    if ep_key in done:
        _log(log_cb, f"⏭️ الحلقة دي اترفعت على تيليجرام قبل كده ({ep_key}) — تخطي.")
        return "already uploaded"

    if not st.get("channel_id"):
        _log(log_cb, "📺 أول رفع: جاري إنشاء القناة الخاصة تلقائياً...")
        msg = ui_ensure_channel(str(st.get("channel_title") or DEFAULT_CHANNEL_TITLE))
        _log(log_cb, msg)
        st = _load_state()
        if not st.get("channel_id"):
            raise RuntimeError("تعذر إنشاء قناة تيليجرام — راجع إعدادات 📡 تيليجرام.")

    caption = build_caption(series, int(ep_num or 0), quality)
    chan_id = int(st["channel_id"])
    total_bytes = p.stat().st_size
    last_sent = {"pct": -5.0, "t": 0.0}

    async def _go() -> int:
        c = await _ensure_client()
        if not await c.is_user_authorized():
            raise RuntimeError("تيليجرام مش مسجل دخول — سجّل من إعدادات 📡 تيليجرام.")
        ent = await c.get_entity(types.PeerChannel(chan_id))

        def _on_progress(sent: int, total: int) -> None:
            pct = (sent * 100.0 / (total or total_bytes or 1))
            now = time.time()
            if pct - last_sent["pct"] >= 3.0 or now - last_sent["t"] >= 4.0 or pct >= 100.0:
                last_sent["pct"], last_sent["t"] = pct, now
                if callable(progress_cb):
                    try:
                        progress_cb(pct, f"{sent / 1048576:.0f} / {(total or total_bytes) / 1048576:.0f} ميجا", "رفع على تيليجرام")
                    except Exception:
                        pass

        attrs = []
        if meta["duration"] > 0:
            attrs.append(types.DocumentAttributeVideo(
                duration=meta["duration"], w=meta["w"] or 1280, h=meta["h"] or 720,
                supports_streaming=True,
            ))
        msg = await c.send_file(
            ent, str(p),
            caption=caption,
            attributes=attrs or None,
            progress_callback=_on_progress,
        )
        return int(getattr(msg, "id", 0) or 0)

    _log(log_cb, f"📡 بدء رفع «{caption.splitlines()[0]}» ({size_mb:.0f} ميجا) على القناة الخاصة...")
    msg_id = _get_runner().run(_go(), timeout=None)
    st = _load_state()
    uploads = list(st.get("uploads") or [])
    uploads.append(ep_key)
    st["uploads"] = uploads[-DEDUPE_KEEP:]
    _save_state(st, backup=False)
    _log(log_cb, f"✅ اترفعت على تيليجرام (رسالة #{msg_id}) — {st.get('channel_title') or ''}")
    return f"https://t.me/c/{chan_id}/{msg_id}" if msg_id else "تم الرفع"
