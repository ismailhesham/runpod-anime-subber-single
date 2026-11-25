import json
import os
import random
import re
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pysubs2

BASE_DIR = Path(__file__).resolve().parent
STYLE_JSON_PATH = BASE_DIR / "translator_style_celestial.json"
GLOSSARY_DIR = Path(os.environ.get("RUNPOD_GLOSSARY_ROOT", "/workspace/glossaries"))


def _glossary_dir() -> Path:
    """Resolve the glossary root: env override -> pod path -> repo-bundled folder."""
    env_root = os.environ.get("RUNPOD_GLOSSARY_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    pod = Path("/workspace/glossaries")
    try:
        pod.mkdir(parents=True, exist_ok=True)
        return pod
    except OSError:
        return BASE_DIR / "glossaries"


def _repo_glossary_template() -> Dict:
    """Bundled starter glossary (repo: glossaries/glossary.example.json), or empty."""
    tmpl = BASE_DIR / "glossaries" / "glossary.example.json"
    try:
        if tmpl.is_file():
            data = json.loads(tmpl.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}
TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text"}

INSTALLED_SAFE_FONTS = {
    "Bahij Nassim",
    "Bahij Mitra",
    "BKR Shahd Extra Bold",
    "AFSANEH_v2 KHWF",
    "Hayah",
    "Loving Stay",
    "Sovba Regular",
}

FONT_FALLBACK_MAP = {
    "AlHurraTxtreg": "Bahij Nassim",
    "SC_REHAN": "Hayah",
    "Bahij SultanNahia": "AFSANEH_v2 KHWF",
    "Berylium": "Sovba Regular",
    "Arabetics Harfi Regular": "Hayah",
    "Arphic PReisho Medium JIS": "Sovba Regular",
    "HSN Shahd Regular": "BKR Shahd Extra Bold",
    "HSN Shahd": "BKR Shahd Extra Bold",
    "khalaad Noora": "Hayah",
    "Catull": "Sovba Regular",
    "FF Dusha Arabic": "Bahij Nassim",
    "bader_al yadawi": "Bahij Mitra",
    "Hacen Tehran": "Bahij Mitra",
    "Arial": "Bahij Mitra",
    "Bahij Tanseek Pro": "Hayah",
}

# Family name inside the user-supplied font file when it differs from the name the
# translator's ASS styles request (fc-match needs the real family, e.g. "HSN Shahd"
# lives in the file as "HSN Shahd Regular").
REAL_FAMILY_ALIAS = {
    "HSN Shahd": "HSN Shahd Regular",
    "FF Dusha": "FF Dusha Arabic",
}

STYLE_PROFILE_VERSION = 3

DEFAULT_CELESTIAL_PROFILE = {
    "profile_version": STYLE_PROFILE_VERSION,
    "translator_profile": "celestial_dragons",
    "linguistic_rules": [
        "فصحى أدبية مكثفة، جمل قصيرة (وسيط ~21 حرف، الحد الأقصى العملي 55 حرفاً بدون تكديس سطرين \\N)",
        "! و ، و ... تُكتب في بداية النص المنطقي لملفات ASS بدون RLE؛ بينما ؟ تبقى في آخر السطر؛ والتعجب مع الاستفهام: !...؟",
        "الجملة المقسومة على حدثين متتاليين: تنتهي الأولى أو تبدأ الثانية بـ ...",
        "الكلام المقطوع فجأة: آخر حرف بالتطويل متبوعاً بـ ... في البداية مثل (...أنا لن أسـ)",
        "التلعثم: الحرف الأول + تطويل وشرطة + الكلمة كاملة مثل (مـ-مهلًا) أو (نحـ-نحتاج)",
        "الألقاب اليابانية تُحفظ بشرطة ملتصقة بالاسم: -سان، -ساما، -تشان، -كن، -دونو، -سينباي، -سينسي، والنداء بـ يا",
        "التشكيل انتقائي وظيفي: تنوين النصب (حقًا، مهلًا، حتمًا) وتشكيل الضمائر للتفريق (أنتَ/أنتِ، لكَ/لكِ)",
        "الشتائم المعتمدة بأسلوب المترجم: أيها الوغد، أيها الوضيع، أيها اللعين، أيها الأحمق، تبًا، سحقًا",
        "حرف چ لصوت j الياباني، وحرف ڤ لصوت v في الأسماء والمصطلحات",
        "مصطلحات العالم الخيالي الخاصة تُكتب بنطقها مع ال التعريف، والمفاهيم الوصفية ذات المعنى تُترجم فصيحاً",
    ],
    "never_copy_errors": {
        "لم يبقى": "لم يبقَ",
        "لا يبقى ": "لا يبقَ ",
        "وحدكي": "وحدكِ",
        "أكان موجود ": "أكان موجودًا ",
        "أن يمضي كلًا": "أن يمضي كلٌّ",
        "اللذين ": "الذين ",
    },
    "few_shot_examples": [
        "!ما لنا غير هذا الآن",
        "!ستعرض نفسك للخطر",
        "،وبعد تكرار هذا عدة مرات",
        "،لن أشرح لكِ أسباب امتناعنا عن ذلك",
        "...المعذرة، تم الإيقاع بي في غفلةٍ مني",
        "...أنا لن أسـ",
        "تحسن التفكير بين الحين والآخر...",
        "هل أنتم بصحة جيدة؟",
        "أولم أخبركِ؟",
        "!من فعل هذا؟",
        "!أتثني على عدوك أثناء سير المعركة؟",
        "ألم تستمع أيها الأحمق؟",
        "!أيها الوغد",
        "لربما نودع أحدنا الآخر",
        "الموت أو الحياة بعد قتالكِ سيان",
        "أليس كذلك يا {NAME}-كن؟",
        "!انظر يا {NAME}-سان",
        "مـ-مهلًا",
    ],
    "responsive_geometry": {
        "bottom_collision_y_pct": 60.0,
        "presets": {
            "black_clover_style": {
                "layout_res_ratio": [0.333333, 0.333333],
                "blur_edge_h_ratio": 0.001852,
                "collision_mode": "style_top",
                "main_style_name": "Main",
                "top_style_name": "Top",
                "event_margin_v_overrides_h_pct": {"OP-Ara": 9.2593, "ED-Ara": 10.1852},
                "styles": {
                    "Main": {"font": "Bahij Nassim", "fs_h": 9.2593, "out_h": 0.2315, "shd_h": 0.0926, "mv_h": 4.3519, "ml_w": 1.5625, "mr_w": 1.5625, "sx": 90, "sy": 100, "an": 2, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H7F000000"},
                    "Top": {"font": "Bahij Nassim", "fs_h": 9.2593, "out_h": 0.2315, "shd_h": 0.0926, "mv_h": 4.3519, "ml_w": 1.5625, "mr_w": 1.5625, "sx": 90, "sy": 100, "an": 8, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H7F000000"},
                    "Narrator": {"font": "Bahij Nassim", "fs_h": 9.2593, "out_h": 0.2315, "shd_h": 0.0926, "mv_h": 5.5556, "ml_w": 1.5625, "mr_w": 1.5625, "sx": 90, "sy": 100, "an": 2, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00112F4A", "back_c": "&H7F000000"},
                    "Narrator - Top": {"font": "Bahij Nassim", "fs_h": 9.2593, "out_h": 0.2315, "shd_h": 0.0926, "mv_h": 2.7778, "ml_w": 1.5625, "mr_w": 1.5625, "sx": 90, "sy": 100, "an": 8, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00112F4A", "back_c": "&H7F000000"},
                    "Note": {"font": "Bahij Mitra", "fs_h": 6.9444, "out_h": 0.2778, "shd_h": 0.0, "mv_h": 1.8519, "ml_w": 0.7812, "mr_w": 0.7812, "sx": 100, "sy": 100, "an": 8, "bold": 0, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H00000000"},
                    "Signs": {"font": "Bahij Mitra", "fs_h": 6.9444, "out_h": 0.1852, "shd_h": 0.0, "mv_h": 0.0, "ml_w": 0.0, "mr_w": 0.0, "sx": 100, "sy": 100, "an": 5, "bold": 0, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H00000000"},
                    "Rights": {"font": "Hayah", "fs_h": 7.4074, "out_h": 0.0926, "shd_h": 0.0, "mv_h": 1.8519, "ml_w": 1.0417, "mr_w": 1.0417, "sx": 100, "sy": 100, "an": 9, "bold": 0, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H00322F2A"},
                    "OP-Ara": {"font": "Hayah", "fs_h": 10.1852, "out_h": 0.1852, "shd_h": 0.0, "mv_h": 1.8519, "ml_w": 0.0, "mr_w": 0.0, "sx": 100, "sy": 100, "an": 2, "bold": 0, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H00FFFFFF"},
                    "OP-Rom": {"font": "Sovba Regular", "fs_h": 7.4074, "out_h": 0.1852, "shd_h": 0.0, "mv_h": 2.7778, "ml_w": 0.0, "mr_w": 0.0, "sx": 100, "sy": 100, "an": 8, "bold": 0, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H00FFFFFF"},
                    "ED-Ara": {"font": "Hayah", "fs_h": 10.1852, "out_h": 0.2778, "shd_h": 0.0, "mv_h": 3.2407, "ml_w": 0.0, "mr_w": 0.0, "sx": 100, "sy": 100, "an": 2, "bold": 0, "pri": "&H00E2F4F3", "out_c": "&H00000000", "back_c": "&H00000000"},
                    "ED-Rom": {"font": "Sovba Regular", "fs_h": 7.4074, "out_h": 0.2778, "shd_h": 0.0, "mv_h": 2.7778, "ml_w": 0.0, "mr_w": 0.0, "sx": 100, "sy": 100, "an": 8, "bold": -1, "pri": "&H00E2F4F3", "out_c": "&H00000000", "back_c": "&H00000000"},
                },
            },
            "bleach_style": {
                "layout_res_ratio": None,
                "blur_edge_h_ratio": 0.002778,
                "collision_mode": "inline_an8",
                "main_style_name": "Main",
                "top_style_name": "Top",
                "event_margin_v_overrides_h_pct": {},
                "styles": {
                    "Main": {"font": "Bahij Nassim", "fs_h": 10.2778, "out_h": 0.2346, "shd_h": 0.1852, "mv_h": 2.7778, "ml_w": 0.0, "mr_w": 0.0, "sx": 95, "sy": 100, "an": 2, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00313131", "back_c": "&HC0636363"},
                    "Top": {"font": "Bahij Nassim", "fs_h": 10.2778, "out_h": 0.2346, "shd_h": 0.1852, "mv_h": 2.7778, "ml_w": 0.0, "mr_w": 0.0, "sx": 95, "sy": 100, "an": 8, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00313131", "back_c": "&HC0636363"},
                    "Narrator": {"font": "AlHurraTxtreg", "fs_h": 10.1852, "out_h": 0.2346, "shd_h": 0.1481, "mv_h": 2.7778, "ml_w": 10.3125, "mr_w": 10.3125, "sx": 95, "sy": 100, "an": 2, "bold": -1, "pri": "&H00E7FCFF", "out_c": "&H001A3F5A", "back_c": "&H641A3F5A"},
                    "Narrator - Top": {"font": "AlHurraTxtreg", "fs_h": 10.1852, "out_h": 0.2346, "shd_h": 0.1481, "mv_h": 2.7778, "ml_w": 10.3125, "mr_w": 10.3125, "sx": 95, "sy": 100, "an": 8, "bold": -1, "pri": "&H00E7FCFF", "out_c": "&H001A3F5A", "back_c": "&H641A3F5A"},
                    "Note": {"font": "Bahij Nassim", "fs_h": 6.9444, "out_h": 0.1389, "shd_h": 0.0, "mv_h": 1.3889, "ml_w": 0.7812, "mr_w": 0.7812, "sx": 100, "sy": 100, "an": 8, "bold": 0, "pri": "&H00E3F7F9", "out_c": "&HD3000000", "back_c": "&H00000000"},
                    "Signs": {"font": "SC_REHAN", "fs_h": 7.4074, "out_h": 0.0, "shd_h": 0.0, "mv_h": 5.5556, "ml_w": 1.5625, "mr_w": 1.5625, "sx": 100, "sy": 100, "an": 2, "bold": -1, "pri": "&H00E5E5E3", "out_c": "&H00959596", "back_c": "&H00000000"},
                    "Attack": {"font": "Bahij SultanNahia", "fs_h": 12.037, "out_h": 0.2469, "shd_h": 0.0247, "mv_h": 1.3889, "ml_w": 0.6771, "mr_w": 0.6771, "sx": 100, "sy": 100, "an": 2, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H32000000", "back_c": "&HB4F2FFFD"},
                    "Rights": {"font": "Hayah", "fs_h": 7.4074, "out_h": 0.0926, "shd_h": 0.0, "mv_h": 1.8519, "ml_w": 1.0417, "mr_w": 1.0417, "sx": 100, "sy": 100, "an": 9, "bold": 0, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H00322F2A"},
                    "OP-Ara": {"font": "SC_REHAN", "fs_h": 8.3333, "out_h": 0.2037, "shd_h": 0.0, "mv_h": 1.8519, "ml_w": 0.0, "mr_w": 0.0, "sx": 100, "sy": 100, "an": 2, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H7F404040"},
                    "OP-Rom": {"font": "Berylium", "fs_h": 8.3333, "out_h": 0.2037, "shd_h": 0.0, "mv_h": 0.9259, "ml_w": 1.5625, "mr_w": 1.5625, "sx": 100, "sy": 100, "an": 8, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H7F404040"},
                    "ED-Ara": {"font": "Arabetics Harfi Regular", "fs_h": 8.7963, "out_h": 0.2037, "shd_h": 0.0, "mv_h": 1.8519, "ml_w": 0.0, "mr_w": 0.0, "sx": 100, "sy": 100, "an": 2, "bold": -1, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H7F404040"},
                    "ED-Rom": {"font": "Arphic PReisho Medium JIS", "fs_h": 7.4074, "out_h": 0.2037, "shd_h": 0.0, "mv_h": 0.9259, "ml_w": 1.5625, "mr_w": 1.5625, "sx": 100, "sy": 100, "an": 8, "bold": 0, "pri": "&H00FFFFFF", "out_c": "&H00000000", "back_c": "&H7F404040"},
                },
            },
        },
    },
}


@dataclass
class SubEntry:
    index: int
    start_ms: int
    end_ms: int
    target_style: str
    actor: str
    plain_text: str
    preserved_tags: str
    keep_untranslated: bool = False
    src_playres_x: int = 1920
    src_playres_y: int = 1080


def ensure_style_profile() -> Dict:
    """Load translator_style_celestial.json or write default Celestial profile if missing."""
    if STYLE_JSON_PATH.is_file():
        try:
            loaded = json.loads(STYLE_JSON_PATH.read_text(encoding="utf-8"))
            if (
                "responsive_geometry" in loaded
                and "presets" in loaded.get("responsive_geometry", {})
                and int(loaded.get("profile_version", 0)) >= STYLE_PROFILE_VERSION
            ):
                return loaded
        except Exception:
            pass
    try:
        STYLE_JSON_PATH.write_text(
            json.dumps(DEFAULT_CELESTIAL_PROFILE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return DEFAULT_CELESTIAL_PROFILE


def _norm_font_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _user_added_font_file(font_name: str) -> bool:
    """True if the user dropped a matching .ttf/.otf into fonts/ (e.g. SC_REHAN.ttf)."""
    wanted = _norm_font_key(font_name)
    if not wanted:
        return False
    try:
        for p in (BASE_DIR / "fonts").glob("*"):
            if p.suffix.lower() not in (".ttf", ".otf"):
                continue
            stem = _norm_font_key(p.stem)
            if stem == wanted or stem.startswith(wanted) or wanted.startswith(stem):
                return True
    except OSError:
        pass
    return False


def _safe_font(font_name: str) -> str:
    """Use the translator's exact font when installed; otherwise fall back gracefully.

    If the user later adds the real font file (e.g. SC_REHAN.ttf) to fonts/, it is
    picked up automatically on the next run — no code change needed.
    """
    if not font_name:
        return "Bahij Nassim"
    if font_name in INSTALLED_SAFE_FONTS:
        return font_name
    if font_name in REAL_FAMILY_ALIAS and _user_added_font_file(REAL_FAMILY_ALIAS[font_name]):
        return REAL_FAMILY_ALIAS[font_name]
    if _user_added_font_file(font_name):
        return font_name
    return FONT_FALLBACK_MAP.get(font_name, "Bahij Nassim")


def _build_celestial_system_prompt(series_name: str = "", glossary: Optional[Dict] = None) -> str:
    prof = ensure_style_profile()
    rules_txt = "\n".join(f"- {r}" for r in prof.get("linguistic_rules", []))
    examples = prof.get("few_shot_examples", [])
    ex_lines = []
    for item in examples[:24]:
        ex_lines.append(f"  • {item['ar'] if isinstance(item, dict) else item}")
    examples_txt = "\n".join(ex_lines)

    glossary_block = ""
    if glossary and any(glossary.get(k) for k in ("names", "transliterate", "translate")):
        glossary_block = (
            "\n\n### المعتَمَد الإلزامي لقاموس هذا الأنمي (التزم به حرفياً لتوحيد الأسماء والمصطلحات):\n"
            + json.dumps(
                {
                    "series": series_name or glossary.get("series", ""),
                    "character_names": glossary.get("names", {}),
                    "transliterate_terms": glossary.get("transliterate", {}),
                    "translate_terms": glossary.get("translate", {}),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif series_name.strip():
        glossary_block = f"\n\n### سياق العمل: هذا الحوار من أنمي «{series_name.strip()}». وحّد كتابة أسماء الشخصيات والتقنيات بدقة."

    return f"""أنت مترجم أنمي عربي محترف تحاكي البصمة اللغوية والأسلوبية لفريق Celestial-Dragons بدقة 95%+.
مهمتك ترجمة مصفوفة JSON من الجمل (من الإنجليزية أو اليابانية المفرّغة صوتياً) إلى العربية الفصحى الأدبية المكثفة.

### القواعد الصارمة لأسلوب المترجم:
{rules_txt}
- ممنوع تماماً استخدام كسر السطر (\\N) داخل جمل الحوار العادي؛ اجعل الصياغة مكثفة ورشيقة في سطر واحد متماسك.
- للستايل "Narrator" أو "Narrator - Top": استخدم سرداً أدبياً فخيماً.
- للستايل "OP-Ara" أو "ED-Ara": استخدم صياغة شاعرية غنائية موزونة.
- للستايل "Signs": اجعل الترجمة موجزة جداً كعنوان لافتة أو اسم مكان أو تقنية دون حشو.
- تجنب الأخطاء النحوية الشائعة (مثل: اكتب «لم يبقَ» وليس «لم يبقى»، «وحدكِ» وليس «وحدكي»).

### نماذج مرجعية حية من أسلوب المترجم في الصياغة وعلامات الترقيم:
{examples_txt}{glossary_block}

### صيغة الإخراج الإلزامية:
أرجع كائن JSON صالحاً فقط (بدون أي شرح أو علامات markdown) يربط كل مفتاح "id" (كنص) بترجمته العربية النهائية."""


def probe_embedded_subtitles(video_path: str) -> Optional[Tuple[int, str]]:
    """Find the best embedded text subtitle stream in an MKV/MP4."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title",
        "-of", "json",
        video_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return None

    streams = json.loads(res.stdout or "{}").get("streams", [])
    candidates = []
    for pos, st in enumerate(streams):
        codec = (st.get("codec_name") or "").lower()
        if codec not in TEXT_SUB_CODECS:
            continue
        tags = st.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        title = (tags.get("title") or "").lower()
        score = (12 if lang in ("eng", "en", "english") else 4) + (6 if codec in ("ass", "ssa") else 0)
        if "sign" in title and "full" not in title:
            score -= 8
        candidates.append((score, pos, codec))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def extract_embedded_subtitle(video_path: str, sub_pos: int, codec: str, out_dir: str) -> str:
    ext = ".ass" if codec in ("ass", "ssa") else ".srt"
    out_path = os.path.join(out_dir, f"extracted_source{ext}")
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-map", f"0:s:{sub_pos}", out_path],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError(f"Failed to extract subtitle: {res.stderr[-300:]}")
    return out_path


def _extract_16k_mono_pcm(video_path: str, wav_path: str) -> np.ndarray:
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        wav_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.isfile(wav_path):
        raise RuntimeError(f"FFmpeg audio extraction failed: {res.stderr[-300:]}")

    with wave.open(wav_path, "rb") as wf:
        raw_bytes = wf.readframes(wf.getnframes())
    try:
        os.remove(wav_path)
    except OSError:
        pass
    return np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def _split_whisper_words_cleanly(seg, max_chars: int = 48) -> List[Tuple[int, int, str]]:
    """Use word_timestamps to avoid long multi-line stacked events without \\N."""
    words = getattr(seg, "words", None) or []
    txt_full = (seg.text or "").strip()
    if not txt_full:
        return []
    if not words or len(txt_full) <= max_chars:
        return [(int(seg.start * 1000), int(seg.end * 1000), txt_full)]

    chunks: List[Tuple[int, int, str]] = []
    cur_words: List[str] = []
    cur_start: Optional[float] = None
    cur_end: float = seg.start

    for w in words:
        w_txt = (w.word or "")
        if cur_start is None:
            cur_start = float(w.start)
        candidate = "".join(cur_words) + w_txt
        if cur_words and len(candidate.strip()) > max_chars:
            part_txt = "".join(cur_words).strip()
            if part_txt:
                chunks.append((int(cur_start * 1000), int(cur_end * 1000), f"{part_txt}..."))
            cur_words = [w_txt]
            cur_start = float(w.start)
            cur_end = float(w.end)
        else:
            cur_words.append(w_txt)
            cur_end = float(w.end)

    if cur_words and cur_start is not None:
        tail_txt = "".join(cur_words).strip()
        if tail_txt:
            prefix = "..." if chunks else ""
            chunks.append((int(cur_start * 1000), int(cur_end * 1000), f"{prefix}{tail_txt}"))

    return chunks or [(int(seg.start * 1000), int(seg.end * 1000), txt_full)]


def transcribe_with_whisper(
    video_path: str,
    out_srt_path: str,
    model_size: str = "large-v3",
    log_cb: Optional[Callable[[str], None]] = None,
) -> str:
    from faster_whisper import WhisperModel

    device, compute_type = "cuda", "float16"
    try:
        import torch
        if not torch.cuda.is_available():
            device, compute_type = "cpu", "int8"
    except Exception:
        pass

    os.makedirs(os.path.dirname(os.path.abspath(out_srt_path)), exist_ok=True)
    tmp_wav = os.path.join(os.path.dirname(os.path.abspath(out_srt_path)), "_whisper_16k.wav")

    if log_cb:
        log_cb("[whisper_prog] 2.0% | استخراج الصوت النقي عبر FFmpeg (16kHz)...")
    audio_array = _extract_16k_mono_pcm(video_path, tmp_wav)
    total_dur = max(0.1, float(len(audio_array)) / 16000.0)

    if log_cb:
        log_cb(f"Loading faster-whisper ({model_size}) on {device} (word_timestamps=True)...")
        log_cb(f"[whisper_prog] 5.0% | تحميل موديل Whisper ({model_size}) على {device}...")

    whisper_cache = os.environ.get("WHISPER_CACHE_DIR", "/workspace/models/whisper")
    os.makedirs(whisper_cache, exist_ok=True)
    model = WhisperModel(model_size, device=device, compute_type=compute_type, download_root=whisper_cache)
    segments, info = model.transcribe(
        audio_array,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    if log_cb:
        log_cb(f"Detected audio language: {info.language} ({info.language_probability:.2f})")

    subs = pysubs2.SSAFile()
    last_emit = 0.0
    for seg in segments:
        for st_ms, en_ms, part_text in _split_whisper_words_cleanly(seg):
            if en_ms <= st_ms:
                en_ms = st_ms + 700
            subs.append(pysubs2.SSAEvent(start=st_ms, end=en_ms, text=part_text, style="Main"))
        now_t = time.monotonic()
        if log_cb and (now_t - last_emit >= 0.5):
            last_emit = now_t
            pct = min(99.5, (float(seg.end) / total_dur) * 100.0)
            cm, cs = divmod(int(seg.end), 60)
            tm, ts = divmod(int(total_dur), 60)
            log_cb(f"[whisper_prog] {pct:.1f}% | {cm:02d}:{cs:02d} / {tm:02d}:{ts:02d}")

    if log_cb:
        log_cb("[whisper_prog] 100.0% | اكتمل التفريغ الصوتي وتقسيم الجمل 100%")
    subs.save(out_srt_path)
    return out_srt_path


def _classify_style(orig_style: str, orig_actor: str, raw_text: str) -> Tuple[str, bool]:
    s, a = (orig_style or "").lower(), (orig_actor or "").lower()
    if re.search(r"\brom|kara", s):
        return ("ED-Rom" if re.search(r"\bed\b|ending", s) else "OP-Rom"), True
    if re.search(r"\bop\b|opening", s):
        return "OP-Ara", False
    if re.search(r"\bed\b|ending", s):
        return "ED-Ara", False
    if "narrat" in s or "narrat" in a:
        return ("Narrator - Top" if ("top" in s or "\\an8" in raw_text) else "Narrator"), False
    if "note" in s or "tl" in a:
        return "Note", False
    if "attack" in s or "technique" in s or "spell" in s:
        return "Attack", False
    if any(k in s for k in ("sign", "title", "caption", "screen", "board")) or "sign" in a or "\\pos(" in raw_text or "\\move(" in raw_text:
        return "Signs", False
    if "top" in s or "overlap" in s or "\\an8" in raw_text:
        return "Top", False
    return "Main", False


def _extract_layout_tags(raw_text: str) -> str:
    tags = []
    for block in re.findall(r"\{([^}]*)\}", raw_text):
        for m in re.finditer(
            r"\\(?:pos|move|fad|fade|org)\([^)]*\)|\\(?:an\d|frz-?[\d.]+|blur[\d.]+|bord[\d.]+|fs[\d.]+|c&H[0-9A-Fa-f]+&|3c&H[0-9A-Fa-f]+&)",
            block,
        ):
            tags.append(m.group(0))
    return "".join(tags)


def parse_source_subtitles(sub_path: str) -> List[SubEntry]:
    subs = pysubs2.load(sub_path, encoding="utf-8")
    src_px = int(float(subs.info.get("PlayResX", 1920) or 1920))
    src_py = int(float(subs.info.get("PlayResY", 1080) or 1080))
    entries: List[SubEntry] = []

    for ev in subs:
        if ev.is_comment:
            continue
        raw = ev.text.strip()
        if not raw or re.search(r"\\p[1-9]", raw):
            continue
        target_style, keep_untranslated = _classify_style(ev.style, ev.name, raw)
        plain = re.sub(r"\{[^}]*\}", "", raw).replace("\\N", " ").replace("\\n", " ").strip()
        plain = re.sub(r"\s{2,}", " ", plain)
        if not plain:
            continue
        entries.append(
            SubEntry(
                index=len(entries),
                start_ms=ev.start,
                end_ms=ev.end,
                target_style=target_style,
                actor=ev.name or "",
                plain_text=plain,
                preserved_tags=_extract_layout_tags(raw) if target_style == "Signs" else "",
                keep_untranslated=keep_untranslated,
                src_playres_x=src_px,
                src_playres_y=src_py,
            )
        )
    return entries


def _call_llm_json(
    system_prompt: str,
    user_payload: str,
    provider: str,
    api_key: str,
    model_name: str,
    base_url: str,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict:
    """Unified LLM JSON caller with DeepSeek primary -> deepseek-v4-pro fallback chain."""
    prov = (provider or "deepseek").lower()
    key = api_key.strip()
    if not key and "local" not in prov:
        raise ValueError("يرجى إدخال مفتاح API Key في خانة الإعدادات أولاً.")

    if "gemini" in prov:
        candidates = [model_name.strip() or "gemini-3.8-flash", "gemini-2.5-flash"]
    elif "deepseek" in prov or "deepseek" in (base_url or "").lower() or "deepseek" in (model_name or "").lower():
        primary = model_name.strip() or "deepseek-flash"
        candidates = []
        for m in (primary, "deepseek-v4-pro", "deepseek-chat"):
            if m and m not in candidates:
                candidates.append(m)
        if not base_url.strip():
            base_url = "https://api.deepseek.com"
    else:
        candidates = [model_name.strip() or "deepseek-flash"]

    last_err: Optional[Exception] = None
    for m_idx, cand_model in enumerate(candidates):
        for attempt in range(5):
            try:
                if "gemini" in prov:
                    from google import genai
                    from google.genai import types
                    client = genai.Client(api_key=key)
                    resp = client.models.generate_content(
                        model=cand_model,
                        contents=user_payload,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            temperature=0.25,
                            response_mime_type="application/json",
                        ),
                    )
                    raw_out = resp.text or "{}"
                else:
                    from openai import OpenAI
                    client = OpenAI(api_key=key or "local-key", base_url=base_url.strip() or None, timeout=180.0, max_retries=0)
                    kwargs = {
                        "model": cand_model,
                        "temperature": 0.25,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_payload},
                        ],
                    }
                    if "deepseek" in (base_url or "").lower() or "openai" in (base_url or "").lower():
                        kwargs["response_format"] = {"type": "json_object"}
                    resp = client.chat.completions.create(**kwargs)
                    raw_out = resp.choices[0].message.content or "{}"

                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_out.strip())
                s, e = cleaned.find("{"), cleaned.rfind("}")
                if s != -1 and e != -1:
                    cleaned = cleaned[s : e + 1]
                return json.loads(cleaned)
            except Exception as exc:
                last_err = exc
                wait_s = min(30.0, 2.0 * (2 ** attempt)) + random.uniform(0.0, 1.5)
                if log_cb:
                    log_cb(f"⚠️ محاولة {attempt + 1}/5 فشلت مع {cand_model} — إعادة المحاولة بعد {wait_s:.0f} ثانية...")
                time.sleep(wait_s)
        if m_idx + 1 < len(candidates) and log_cb:
            log_cb(f"⚠️ تعذر الاستجابة من {cand_model} — جاري التحويل التلقائي للبديل {candidates[m_idx + 1]}...")

    raise RuntimeError(f"فشل الاتصال بجميع النماذج ({candidates}): {last_err}")


def _glossary_slug(series_name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", (series_name or "general_anime").strip().lower()).strip("_")
    return slug or "general_anime"


def save_series_glossary(series_name: str, glossary_data: Dict, glossary_root: Optional[Path] = None) -> str:
    root = glossary_root or _glossary_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_glossary_slug(series_name)}.json"
    path.write_text(json.dumps(glossary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def learn_glossary_from_translation(
    series_name: str,
    entries: List["SubEntry"],
    translations: Dict[int, str],
    glossary: Optional[Dict],
    provider: str,
    api_key: str,
    model_name: str,
    base_url: str,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict:
    """After an episode is translated, persist the actual name/term choices back into the
    per-series glossary file and the in-memory dict, so later episodes stay consistent.
    Existing entries always win; only new keys are added. Never breaks the pipeline."""
    merged = glossary if isinstance(glossary, dict) else {}
    if not translations or not (api_key.strip() or "local" in (provider or "").lower()):
        return merged
    pairs = [
        {"en": e.plain_text, "ar": translations[e.index]}
        for e in entries
        if e.index in translations and not e.keep_untranslated
    ][:120]
    if not pairs:
        return merged

    sys_p = f"""أنت محرّر قواميس ترجمة أنمي بأسلوب فريق Celestial-Dragons (چ لصوت j و ڤ لصوت v).
من أزواج (النص الأصلي ↔ الترجمة العربية النهائية) للأنمي «{series_name or 'غير محدد'}» استخرج فقط الأعلام والتقنيات والرتب والمفاهيم الخاصة كما تُرجمت فعلياً، وأرجع JSON فقط بهذا الهيكل:
{{
  "names": {{"SourceName": "الاسم بالعربية"}},
  "transliterate": {{"SpecialTerm": "المصطلح بنطقه مع ال"}},
  "translate": {{"WorldConcept": "الترجمة الفصيحة"}}
}}
تجاهل الكلمات العادية والعبارات العامة، واقتصر على ما ظهر فعلاً في الأزواج."""

    try:
        extracted = _call_llm_json(
            sys_p,
            json.dumps(pairs, ensure_ascii=False),
            provider=provider,
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            log_cb=log_cb,
        )
        merged.setdefault("series", series_name.strip() or "أنمي عام")
        merged.setdefault("keep_honorifics", True)
        added = 0
        for section in ("names", "transliterate", "translate"):
            merged.setdefault(section, {})
            new_items = extracted.get(section) or {}
            if isinstance(new_items, dict):
                for k, v in new_items.items():
                    k_s, v_s = str(k).strip(), str(v).strip()
                    if k_s and v_s and k_s not in merged[section]:
                        merged[section][k_s] = v_s
                        added += 1
        if added:
            save_series_glossary(series_name, merged)
            if log_cb:
                log_cb(f"📚 القاموس اتعلم تلقائياً: +{added} مصطلح جديد محفوظ للحلقات الجاية.")
    except Exception as exc:
        if log_cb:
            log_cb(f"⚠️ تم تخطي التعلم التلقائي للقاموس: {exc}")
    return merged


def load_or_extract_series_glossary(
    series_name: str,
    entries: List[SubEntry],
    is_raw_whisper: bool,
    provider: str,
    api_key: str,
    model_name: str,
    base_url: str,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict:
    """Load saved per-series glossary or extract new terms from ASS/SRT or Whisper JP audio."""
    g_dir = _glossary_dir()
    g_dir.mkdir(parents=True, exist_ok=True)
    g_path = g_dir / f"{_glossary_slug(series_name)}.json"
    existing: Dict = {
        "series": series_name.strip() or "أنمي عام",
        "names": {},
        "transliterate": {},
        "translate": {},
        "keep_honorifics": True,
    }
    if g_path.is_file():
        try:
            existing.update(json.loads(g_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    else:
        template = _repo_glossary_template()
        for section in ("names", "transliterate", "translate"):
            t_items = template.get(section) or {}
            if isinstance(t_items, dict):
                existing[section].update({str(k): str(v) for k, v in t_items.items()})

    if g_path.is_file() and any(existing.get(s) for s in ("names", "transliterate", "translate")):
        if log_cb:
            n_terms = sum(len(existing.get(s) or {}) for s in ("names", "transliterate", "translate"))
            log_cb(
                f"📚 قاموس «{existing['series']}» محمّل من الحفظ ({n_terms} مصطلح) — "
                "مفيش استخراج من الأول، والتعلم التلقائي بيضيف الجديد بعد كل حلقة."
            )
        return existing

    sample_lines = [e.plain_text for e in entries if not e.keep_untranslated][:160]
    if not sample_lines:
        return existing

    src_desc = (
        "تفريغ صوتي ياباني خام من Whisper (انتبه لتصحيح الأسماء المسموعة صوتياً والألقاب مثل さん/様/くん/ちゃん/殿)"
        if is_raw_whisper
        else "ملف ترجمة نصي"
    )
    sys_p = f"""أنت خبير أنمي ومحرّر قواميس ترجمة احترافية بأسلوب فريق Celestial-Dragons (استخدم چ لصوت j و ڤ لصوت v).
استخرج من عينة النص ({src_desc}) للأنمي «{series_name or 'غير محدد'}» أهم أسماء الشخصيات والمصطلحات الخاصة، وأرجع JSON فقط بهذا الهيكل:
{{
  "names": {{"SourceName": "الاسم بالعربية"}},
  "transliterate": {{"SpecialTerm": "المصطلح بنطقه مع ال مثل البانكاي"}},
  "translate": {{"WorldConcept": "الترجمة الفصيحة مثل مجتمع الأرواح"}}
}}
لا تكرر كلمات عادية، واقتصر على الأعلام والتقنيات والرتب والمفاهيم الخاصة بالعمل."""

    try:
        if log_cb:
            log_cb(f"📚 استخراج وتحديث قاموس الأنمي ({existing['series']})...")
        extracted = _call_llm_json(
            sys_p,
            json.dumps(sample_lines, ensure_ascii=False),
            provider=provider,
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            log_cb=log_cb,
        )
        for section in ("names", "transliterate", "translate"):
            new_items = extracted.get(section) or {}
            if isinstance(new_items, dict):
                for k, v in new_items.items():
                    if k not in existing[section] and str(v).strip():
                        existing[section][str(k).strip()] = str(v).strip()
        save_series_glossary(series_name, existing)
    except Exception as exc:
        if log_cb:
            log_cb(f"⚠️ تم تخطي تحديث القاموس التلقائي: {exc}")

    return existing


def translate_detected_signs(
    signs: List[Dict],
    entries: List[SubEntry],
    series_name: str,
    glossary: Optional[Dict],
    provider: str,
    api_key: str,
    model_name: str,
    base_url: str,
    log_cb: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    """Translate OCR-detected Japanese screen signs with scene dialogue context."""
    if not signs:
        return signs

    payload_items = []
    for sg in signs:
        st_ms, en_ms = sg["start_ms"], sg["end_ms"]
        nearby = [
            e.plain_text
            for e in entries
            if abs(e.start_ms - st_ms) <= 6000 and not e.keep_untranslated
        ][:4]
        payload_items.append(
            {
                "id": str(sg["id"]),
                "jp_sign": sg["jp_text"],
                "nearby_dialogue": nearby,
            }
        )

    glossary_hint = json.dumps(glossary or {}, ensure_ascii=False) if glossary else "{}"
    sys_p = f"""أنت مختص محاكاة وتنضيد لافتات أنمي (Typesetter) لفريق Celestial-Dragons.
ترجم اللافتات اليابانية الملتقطة من الشاشة للأنمي «{series_name or 'عام'}» إلى عناوين عربية فصيحة ومكثفة جداً (كلمة إلى 4 كلمات كحد أقصى لتناسب مساحة اللافتة الممسوحة على الشاشة بدون أي علامات ترقيم زائدة).
استعن بالحوار المحيط والقاموس المرفق لفهم السياق بدقة:
{glossary_hint}
أرجع كائن JSON فقط يربط كل "id" بالنص العربي الموجز للافتة."""

    try:
        if log_cb:
            log_cb(f"🎌 ترجمة {len(signs)} لافتة يابانية مكتشفة عبر الذكاء الاصطناعي...")
        res = _call_llm_json(
            sys_p,
            json.dumps(payload_items, ensure_ascii=False),
            provider=provider,
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            log_cb=log_cb,
        )
        for sg in signs:
            ar_val = str(res.get(str(sg["id"]), "") or "").strip()
            if ar_val:
                sg["ar_text"] = ar_val
    except Exception as exc:
        if log_cb:
            log_cb(f"⚠️ تعذر ترجمة بعض اللافتات تلقائياً (يمكنك كتابتها في جدول المراجعة): {exc}")

    return signs


_MAX_LINE_CHARS = 38  # عرف الفانسب: ~40 حرفاً عربياً للسطر المرئي الواحد (خط Bahij Nassim عريض)


def _split_long_arabic_line(t: str, max_len: int = _MAX_LINE_CHARS) -> List[str]:
    """Balanced 2-3 visual-line wrap for long Arabic dialogue (\\N inserted by the caller).
    Breaks at word boundaries, preferring punctuation edges and balanced halves."""
    t = t.strip()
    if len(t) <= max_len or " " not in t:
        return [t]
    words = t.split(" ")

    def _cost(a: str, b: str) -> float:
        over = max(0, len(a) - (max_len + 4)) + max(0, len(b) - (max_len + 4))
        balance = abs(len(a) - len(b))
        punct_bonus = 6.0 if re.search(r"(?:\.{2,}|[،؟?!.:؛…-])$", a) else 0.0
        orphan = 100.0 if len(a.strip(".!،؟ ")) < 3 or len(b.strip(".!،؟ ")) < 3 else 0.0
        return balance + over * 20.0 + orphan - punct_bonus

    best_i, best_c = 1, None
    for i in range(1, len(words)):
        c = _cost(" ".join(words[:i]), " ".join(words[i:]))
        if best_c is None or c < best_c:
            best_c, best_i = c, i
    first = " ".join(words[:best_i])
    rest = " ".join(words[best_i:])
    segs = [first]
    # سطر ثالث فقط عند طول مبالغ فيه (أكثر من سطرين كاملين تقريباً)
    if len(rest) > max_len + 4:
        segs.extend(_split_long_arabic_line(rest, max_len))
    else:
        segs.append(rest)
    return segs


def _fix_segment_punctuation(seg: str, lead_hint: Optional[bool], trail_hint: Optional[bool],
                             src_shout: bool = False, soften: bool = True) -> str:
    """Celestial punctuation for ONE visual line: trailing !/، to the logical start, ؟ stays at
    the end, and edge-ellipsis placement. Verified libass behavior (line treated as LTR-base):
      "...نص" بدون علامات  ← النقاط تظهر يساراً (نهاية السطر)   [أسلوب Celestial للكلام المبتور]
      "نص..." بدون علامات  ← النقاط تظهر يميناً (بداية السطر)
      RLM + "...نص"        ← يميناً   |   "نص..." + RLM ← يساراً
    نقاط البداية (تكملة من سطر سابق) تُثبَّت يميناً بـ RLM، ونقاط النهاية تُكتب "..." في
    البداية المنطقية بدون علامات فتظهر يساراً. موضع النقاط في المصدر الإنجليزي هو الفيصل؛
    lead_hint/trail_hint: هل يبدأ/ينتهي المصدر بـ '...' — None تعني لا معلومات."""
    t = seg.strip()
    if not t:
        return t
    t = t.replace("?", "؟")
    t = re.sub(r"([؟!])\.+$", r"\1", t)  # "؟." / "!." الزائدة → "؟" / "!"
    if soften:
        # حذف "!" بعد التعجبات اللفظية الخالصة في بداية المقطع (أوه/آه) — طلبه بالاسم
        t = re.sub(r"^(\s*(?:أوه+|اوه+|آ+ه+|أ+ه+))!(?=\s|$)", r"\1", t)
    if re.search(r"(?:!\s*؟|؟\s*!)$", t):
        t = re.sub(r"(?:!\s*؟|؟\s*!)$", "؟", t).strip()
        if not t.startswith("!"):
            t = f"!{t}"
    else:
        m_bang = re.search(r"!+$", t)
        if m_bang and not t.startswith("!"):
            run = m_bang.group(0)
            core_b = t[: m_bang.start()].rstrip()
            if soften and len(run) == 1 and not src_shout:
                t = f"{core_b}."  # تعجب خبري → نقطة (تُنقل للبداية المنطقية في قاعدة النقطة بالأسفل)
            else:
                t = f"{run}{core_b}"  # صياح حقيقي (!! أو مصدر ALL-CAPS) يحتفظ بالعلامة
        elif t.endswith("،") and not t.startswith("،"):
            t = f"،{t.rstrip('، ').strip()}"

    # نقطة/فاصلة نهاية السطر: علامات ضعيفة في سطر LTR-Base فتظهر يميناً (بداية الجملة) —
    # تُنقل للبداية المنطقية فتظهر يساراً في نهايتها الصحيحة (مثبت برندر libass على سطره بالظبط)
    if re.search(r"(?<!\.)\.$", t) and not t.startswith("."):
        t = "." + t[:-1].rstrip()
    elif t.endswith(",") and not t.startswith("،"):
        t = f"،{t.rstrip(', ').strip()}"

    RLM = "\u200f"
    t = re.sub(r"^\s*\.{2,}\s*", "...", t)
    t = re.sub(r"\s*\.{2,}\s*$", "...", t)
    has_lead = t.startswith("...")
    has_trail = t.endswith("...")
    if not (has_lead or has_trail):
        return t
    core = t[3:] if has_lead else t
    if has_trail and core.endswith("..."):
        core = core[:-3]
    core = core.strip()
    if not core:
        return t
    lead_sem = has_lead if lead_hint is None else lead_hint
    trail_sem = has_trail if trail_hint is None else trail_hint
    if lead_sem and trail_sem:
        return f"{RLM}...{core}...{RLM}"
    if lead_sem:
        return f"{RLM}...{core}"
    if trail_sem:
        return f"...{core}"
    # لا معلومات حاسمة: ثبّت كل طرف في موضعه المرئي المقصود
    pre = f"{RLM}..." if has_lead else ""
    post = f"...{RLM}" if has_trail else ""
    return f"{pre}{core}{post}"


def _enforce_celestial_punctuation(
    text: str, style_name: str, source_text: str = "", wrap: bool = True, as_segments: bool = False,
    soften: bool = True,
):
    """Deterministic post-processor enforcing Celestial-Dragons non-RLE libass punctuation & grammar.
    wrap=True يلف الأسطر الطويلة إلى سطرين مرئيين؛ as_segments=True يرجع قائمة المقاطع بدل نص واحد
    (لوضع التقسيم التتابعي الذي يحوّل المقاطع إلى أحداث Dialogue متتالية زمنياً)."""
    prof = ensure_style_profile()
    t = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", (text or "")).strip()
    if style_name in ("Main", "Top", "Narrator", "Narrator - Top"):
        t = t.replace("\\N", " ").replace("\n", " ")
    else:
        t = t.replace("\n", "\\N")
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = t.replace("…", "...")

    for wrong, right in (prof.get("never_copy_errors") or {}).items():
        t = t.replace(wrong, right)

    if not re.search(r"[\u0600-\u06FF]", t) or style_name == "Signs":
        return [t] if as_segments else t

    # لفّ السطر الطويل أولاً ثم طبّق قواعد التنقيط على كل سطر مرئي على حدة،
    # حتى لا تهاجر علامة (! أو ، أو ...) المنقولة للبداية المنطقية إلى سطر مرئي غير سطرها.
    src = (source_text or "").replace("…", "...")
    src_lead = bool(re.match(r"^\s*(?:\.\s*){2,}", src))
    src_trail = bool(re.search(r"(?:\.\s*){2,}\s*[\"'»)\]]*$", src))

    segs = _split_long_arabic_line(t) if wrap else [t]
    if len(segs) > 1 and src_trail and not src_lead:
        m = re.match(r"^\s*\.{2,}\s*", segs[0])
        if m:
            # نقاط النهاية المكتوبة بالأسلوب الكنسي في بداية السطر المنطقي تنتقل بعد اللفّ
            # إلى السطر المرئي الأخير حيث موضعها الصحيح
            segs[0] = segs[0][m.end():]
            segs[-1] = segs[-1].rstrip() + "..."

    # دليل الصياح من المصدر: تعجب مكرر أو كلمة ALL-CAPS — يخص المقطع الأخير فقط
    src_shout = bool(re.search(r"!{2,}\s*$", src)) or bool(
        re.search(r"(?:^|\s)[A-Z]{3,}(?=[!?.,\s]|$)", src)
    )
    any_hints = src_lead or src_trail
    out: List[str] = []
    last_i = len(segs) - 1
    for i, seg in enumerate(segs):
        lead_hint: Optional[bool] = (src_lead if i == 0 else False) if any_hints else None
        trail_hint: Optional[bool] = (src_trail if i == last_i else False) if any_hints else None
        out.append(_fix_segment_punctuation(
            seg, lead_hint, trail_hint, src_shout=(src_shout and i == last_i), soften=soften
        ))
    return out if as_segments else "\\N".join(out)


def translate_entries(
    entries: List[SubEntry],
    provider: str = "deepseek",
    api_key: str = "",
    model_name: str = "deepseek-flash",
    base_url: str = "https://api.deepseek.com",
    series_name: str = "",
    glossary: Optional[Dict] = None,
    checkpoint_path: Optional[str] = None,
    batch_size: int = 35,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[int, str]:
    to_translate = [e for e in entries if not e.keep_untranslated]
    if not to_translate:
        return {}

    translations: Dict[int, str] = {}
    if checkpoint_path and os.path.isfile(checkpoint_path):
        try:
            saved_cp = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
            translations = {int(k): str(v) for k, v in saved_cp.items()}
            if translations and log_cb:
                log_cb(f"♻️ استكمال تلقائي من نقطة الحفظ ({len(translations)} سطر مترجم مسبقاً)...")
        except Exception:
            translations = {}

    remaining = [e for e in to_translate if e.index not in translations]
    if not remaining:
        return translations

    sys_prompt = _build_celestial_system_prompt(series_name=series_name, glossary=glossary)
    total_batches = (len(remaining) + batch_size - 1) // batch_size

    def _save_ckpt() -> None:
        if checkpoint_path:
            try:
                Path(checkpoint_path).write_text(
                    json.dumps({str(k): v for k, v in translations.items()}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass

    def _do_batch(b_idx: int) -> Dict[int, str]:
        chunk = remaining[b_idx * batch_size : (b_idx + 1) * batch_size]
        payload = json.dumps(
            [{"id": str(e.index), "style": e.target_style, "actor": e.actor, "text": e.plain_text} for e in chunk],
            ensure_ascii=False,
        )
        parsed = _call_llm_json(
            system_prompt=sys_prompt,
            user_payload=payload,
            provider=provider,
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            log_cb=log_cb,
        )
        out: Dict[int, str] = {}
        for k, v in parsed.items():
            try:
                out[int(k)] = str(v)
            except ValueError:
                continue
        return out

    workers = min(3, total_batches)
    if workers > 1:
        if log_cb:
            log_cb(f"⚡ ترجمة متوازية: {workers} دفعات معاً في نفس الوقت ({total_batches} دفعة إجمالاً)")
        from concurrent.futures import ThreadPoolExecutor, as_completed

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_do_batch, b_idx) for b_idx in range(total_batches)]
            for fut in as_completed(futures):
                translations.update(fut.result())
                done += 1
                _save_ckpt()
                if log_cb:
                    log_cb(f"Translated batch {done}/{total_batches}")
    else:
        for b_idx in range(total_batches):
            translations.update(_do_batch(b_idx))
            _save_ckpt()
            if log_cb:
                log_cb(f"Translated batch {b_idx + 1}/{total_batches}")

    return translations


def _ms_to_ass_time(ms: int) -> str:
    ms = max(0, int(ms))
    return f"{ms // 3600000}:{(ms // 60000) % 60:02d}:{(ms // 1000) % 60:02d}.{(ms // 10) % 100:02d}"


def _hex_to_ass_bgr(hex_color: str, default: str = "&HFFFFFF&") -> str:
    c = (hex_color or "").strip()
    if c.upper().startswith("&H"):
        return c if c.endswith("&") else f"{c}&"
    c = c.lstrip("#")
    if len(c) == 6 and re.fullmatch(r"[0-9A-Fa-f]{6}", c):
        rr, gg, bb = c[0:2].upper(), c[2:4].upper(), c[4:6].upper()
        return f"&H{bb}{gg}{rr}&"
    return default


def _remap_ass_tags_to_resolution(tags: str, src_w: int, src_h: int, dst_w: int, dst_h: int) -> str:
    if not tags:
        return ""
    sx = dst_w / max(1, src_w)
    sy = dst_h / max(1, src_h)

    def _scale_pair(m: re.Match) -> str:
        cmd = m.group(1)
        nums = [float(x.strip()) for x in m.group(2).split(",")]
        if cmd in ("pos", "org") and len(nums) >= 2:
            return f"\\{cmd}({nums[0] * sx:.1f},{nums[1] * sy:.1f})"
        if cmd == "move" and len(nums) >= 4:
            rest = "," + ",".join(str(int(x)) for x in nums[4:]) if len(nums) > 4 else ""
            return f"\\move({nums[0] * sx:.1f},{nums[1] * sy:.1f},{nums[2] * sx:.1f},{nums[3] * sy:.1f}{rest})"
        return m.group(0)

    remapped = re.sub(r"\\(pos|org|move)\(([^)]+)\)", _scale_pair, tags)
    remapped = re.sub(
        r"\\fs([\d.]+)",
        lambda m: f"\\fs{max(12.0, float(m.group(1)) * sy):.1f}",
        remapped,
    )
    remapped = re.sub(
        r"\\bord([\d.]+)",
        lambda m: f"\\bord{max(0.5, float(m.group(1)) * sy):.2f}",
        remapped,
    )
    return remapped


def _extract_sign_y_pct(entry: SubEntry) -> Optional[float]:
    m = re.search(r"\\pos\(([\d.]+),([\d.]+)\)", entry.preserved_tags or "")
    if m:
        return (float(m.group(2)) / max(1, entry.src_playres_y)) * 100.0
    if "\\an2" in (entry.preserved_tags or ""):
        return 85.0
    return None


def _build_responsive_header(target_w: int, target_h: int, preset_cfg: Dict) -> str:
    layout_ratio = preset_cfg.get("layout_res_ratio")
    layout_lines = ""
    if layout_ratio and len(layout_ratio) == 2:
        lx = max(1, round(target_w * float(layout_ratio[0])))
        ly = max(1, round(target_h * float(layout_ratio[1])))
        layout_lines = f"LayoutResX: {lx}\nLayoutResY: {ly}\n"

    lines = [
        "[Script Info]",
        "; Responsive Celestial-Dragons Style Profile (%W / %H Scaled)",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {target_w}",
        f"PlayResY: {target_h}",
        f"{layout_lines}YCbCr Matrix: TV.709".rstrip("\n"),
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]

    for st_name, s in preset_cfg["styles"].items():
        font = _safe_font(s["font"])
        fs = round(target_h * float(s["fs_h"]) / 100.0, 1)
        out_px = round(target_h * float(s["out_h"]) / 100.0, 2)
        shd_px = round(target_h * float(s["shd_h"]) / 100.0, 2)
        mv = round(target_h * float(s["mv_h"]) / 100.0)
        ml = round(target_w * float(s["ml_w"]) / 100.0)
        mr = round(target_w * float(s["mr_w"]) / 100.0)
        lines.append(
            f"Style: {st_name},{font},{fs},{s['pri']},&H0000FFFF,{s['out_c']},{s['back_c']},"
            f"{s['bold']},0,0,0,{s['sx']},{s['sy']},0,0,1,{out_px},{shd_px},{s['an']},{ml},{mr},{mv},1"
        )

    lines.extend(
        [
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )
    return "\n".join(lines)


def write_responsive_ass(
    entries: List[SubEntry],
    translations: Dict[int, str],
    out_ass_path: str,
    target_w: int = 1920,
    target_h: int = 1080,
    style_preset: str = "bleach_style",
    approved_visual_signs: Optional[List[Dict]] = None,
    credits: Optional[Dict[str, str]] = None,
    wrap_mode: str = "sequential",
    soften_exclaim: bool = True,
    preserve_wording: bool = False,
) -> str:
    prof = ensure_style_profile()
    geom = prof["responsive_geometry"]
    preset_key = style_preset if style_preset in geom["presets"] else "bleach_style"
    preset_cfg = geom["presets"][preset_key]
    be_val = max(1, round(target_h * float(preset_cfg.get("blur_edge_h_ratio", 0.001852))))
    bottom_zone_pct = float(geom.get("bottom_collision_y_pct", 60.0))
    mv_overrides = preset_cfg.get("event_margin_v_overrides_h_pct") or {}

    visual_signs = [s for s in (approved_visual_signs or []) if s.get("enabled", True) and str(s.get("ar_text", "")).strip()]

    bottom_sign_intervals: List[Tuple[int, int]] = []
    for vs in visual_signs:
        if float(vs.get("cy_pct", 50.0)) >= bottom_zone_pct:
            bottom_sign_intervals.append((int(vs["start_ms"]), int(vs["end_ms"])))

    for ev in entries:
        if ev.target_style == "Signs":
            yp = _extract_sign_y_pct(ev)
            if yp is not None and yp >= bottom_zone_pct:
                bottom_sign_intervals.append((ev.start_ms, ev.end_ms))

    lines = [_build_responsive_header(target_w, target_h, preset_cfg)]
    active_bottom_dialogue_end = -1

    for entry in entries:
        if entry.target_style == "Signs" and visual_signs:
            dup = False
            for vs in visual_signs:
                ov = min(entry.end_ms, int(vs["end_ms"])) - max(entry.start_ms, int(vs["start_ms"]))
                if ov >= 250:
                    yp = _extract_sign_y_pct(entry)
                    if yp is None or abs(yp - float(vs.get("cy_pct", 50.0))) <= 18.0:
                        dup = True
                        break
            if dup:
                continue

        raw_final = entry.plain_text if entry.keep_untranslated else translations.get(entry.index, entry.plain_text)
        if preserve_wording:
            seq_segs = None
            final_text = raw_final.replace("\n", r"\N")
        else:
            # التقسيم التتابعي (أسلوب Celestial): السطر الطويل يصبح حدثين متتاليين زمنياً بدل سطر مكدس
            seq_segs: Optional[List[str]] = None
            if (
                wrap_mode == "sequential"
                and not entry.keep_untranslated
                and entry.target_style in ("Main", "Top", "Narrator", "Narrator - Top")
                and (entry.end_ms - entry.start_ms) >= 1700
            ):
                _cand = _enforce_celestial_punctuation(
                    raw_final, entry.target_style, source_text=entry.plain_text, as_segments=True,
                    soften=soften_exclaim,
                )
                if len(_cand) >= 2:
                    seq_segs = _cand
            if seq_segs is None:
                final_text = _enforce_celestial_punctuation(
                    raw_final, entry.target_style, source_text=entry.plain_text,
                    wrap=(wrap_mode == "stacked"), soften=soften_exclaim,
                )
            else:
                final_text = ""

        eff_style = entry.target_style
        inline_an8 = ""

        if eff_style in ("Main", "Narrator"):
            hits_bottom_sign = any(
                min(entry.end_ms, s_en) - max(entry.start_ms, s_st) >= 200
                for s_st, s_en in bottom_sign_intervals
            )
            hits_other_speaker = (active_bottom_dialogue_end - entry.start_ms) >= 280

            if hits_bottom_sign or hits_other_speaker:
                if preset_cfg.get("collision_mode") == "inline_an8":
                    inline_an8 = "\\an8"
                else:
                    eff_style = "Narrator - Top" if eff_style == "Narrator" else "Top"
            else:
                active_bottom_dialogue_end = max(active_bottom_dialogue_end, entry.end_ms)

        ev_mv = 0
        if eff_style in mv_overrides:
            ev_mv = round(target_h * float(mv_overrides[eff_style]) / 100.0)

        if eff_style in ("Main", "Top", "Narrator", "Narrator - Top", "Note"):
            formatted = f"{{{inline_an8}\\be{be_val}}}{final_text}"
        elif eff_style in ("OP-Ara", "OP-Rom"):
            formatted = f"{{\\fad(150,150)\\be{be_val}}}{final_text}"
        elif eff_style in ("ED-Ara", "ED-Rom"):
            formatted = f"{{\\fad(100,100)\\be{be_val}}}{final_text}"
        elif eff_style == "Signs":
            scaled_tags = _remap_ass_tags_to_resolution(
                entry.preserved_tags, entry.src_playres_x, entry.src_playres_y, target_w, target_h
            )
            blur_px = max(1.0, round(target_h * 0.0018, 1))
            bord_px = max(1.0, round(target_h * 0.0015, 1))
            # المترجم يستخدم SC_REHAN لكل اللافتات (بدون إطار)؛ AFSANEH لعناوين الحلقات فقط
            if "title" in entry.actor.lower() or "episode" in entry.actor.lower():
                f_tag = f"\\fnAFSANEH_v2 KHWF\\blur{blur_px}"
            else:
                f_tag = f"\\fn{_safe_font('SC_REHAN')}\\blur{blur_px}"
            formatted = f"{{{f_tag}{scaled_tags}}}{final_text}"
        else:
            formatted = final_text

        layer = 2 if eff_style in ("Main", "Top", "Narrator", "Narrator - Top") else 0
        if seq_segs is not None:
            seg_prefix = f"{{{inline_an8}\\be{be_val}}}"
            dur_ms = entry.end_ms - entry.start_ms
            total_chars = sum(max(1, len(s)) for s in seq_segs)
            bounds = [entry.start_ms]
            acc = 0
            for s in seq_segs[:-1]:
                acc += max(1, len(s))
                bounds.append(entry.start_ms + round(dur_ms * acc / total_chars))
            bounds.append(entry.end_ms)
            min_cue = 700
            for k in range(1, len(bounds) - 1):
                lo = entry.start_ms + min_cue * k
                hi = entry.end_ms - min_cue * (len(seq_segs) - k)
                bounds[k] = max(lo, min(hi, bounds[k]))
            for k, seg_text in enumerate(seq_segs):
                if bounds[k + 1] - bounds[k] < 350:
                    continue
                lines.append(
                    f"Dialogue: {layer},{_ms_to_ass_time(bounds[k])},{_ms_to_ass_time(bounds[k + 1])},"
                    f"{eff_style},{entry.actor},0,0,{ev_mv},,{seg_prefix}{seg_text}"
                )
        else:
            lines.append(
                f"Dialogue: {layer},{_ms_to_ass_time(entry.start_ms)},{_ms_to_ass_time(entry.end_ms)},"
                f"{eff_style},{entry.actor},0,0,{ev_mv},,{formatted}"
            )

    for vs in visual_signs:
        ar_sign = str(vs["ar_text"]).strip()
        cx = round(target_w * float(vs.get("cx_pct", 50.0)) / 100.0, 1)
        cy = round(target_h * float(vs.get("cy_pct", 50.0)) / 100.0, 1)
        box_h_px = max(22.0, target_h * float(vs.get("h_pct", 6.0)) / 100.0)
        box_w_px = max(60.0, target_w * float(vs.get("w_pct", 25.0)) / 100.0)

        est_fs_by_h = box_h_px * 1.08
        est_fs_by_w = (box_w_px / max(3.5, len(ar_sign) * 0.52)) * 1.15
        sign_fs = round(max(target_h * 0.032, min(est_fs_by_h, est_fs_by_w, target_h * 0.145)), 1)

        c_fill = _hex_to_ass_bgr(vs.get("fill_hex", "#FFFFFF"), "&HFFFFFF&")
        c_out = _hex_to_ass_bgr(vs.get("outline_hex", "#000000"), "&H000000&")
        bord_px = round(max(1.2, sign_fs * 0.045), 2)
        blur_px = round(max(0.8, target_h * 0.0014), 2)
        fad_tag = "\\fad(180,180)" if vs.get("has_fade", False) else ""
        ang = float(vs.get("angle_deg", 0.0) or 0.0)
        frz_tag = f"\\frz{-ang:.1f}" if abs(ang) >= 2.0 else ""

        if float(vs.get("h_pct", 6.0)) >= 8.0:
            sign_font = "AFSANEH_v2 KHWF"
        elif any(k in ar_sign for k in ("مملكة", "قصر", "قرية", "مدينة", "غابة", "قاعدة", "طابق")):
            sign_font = "BKR Shahd Extra Bold"
        else:
            sign_font = "BKR Shahd Extra Bold" if len(ar_sign) <= 14 else _safe_font("SC_REHAN")

        vs_txt = (
            f"{{\\an5\\pos({cx},{cy})\\fn{sign_font}\\fs{sign_fs}"
            f"\\c{c_fill}\\3c{c_out}\\bord{bord_px}\\blur{blur_px}{frz_tag}{fad_tag}}}{ar_sign}"
        )
        lines.append(
            f"Dialogue: 1,{_ms_to_ass_time(int(vs['start_ms']))},{_ms_to_ass_time(int(vs['end_ms']))},"
            f"Signs,VisualInpaint,0,0,0,,{vs_txt}"
        )

    if credits:
        slots = [
            ("translator", "ترجمة", "0:00:05.00", "0:00:09.00", 24.22, 74.63),
            ("typesetter", "محاكاة", "0:00:09.20", "0:00:13.00", 57.29, 36.30),
            ("encoder", "دمج ورفع", "0:00:13.20", "0:00:17.50", 24.38, 56.11),
        ]
        fs_en = round(target_h * (55.0 / 1080.0), 1)
        fs_ar = round(target_h * (80.0 / 1080.0), 1)
        bord_cr = round(max(0.4, target_h * (0.5 / 1080.0)), 2)
        for key, ar_label, st, en, xp, yp in slots:
            val = (credits.get(key) or "").strip()
            if val:
                px = round(target_w * xp / 100.0, 1)
                py = round(target_h * yp / 100.0, 1)
                txt = (
                    f"{{\\fad(250,250)\\fnLoving Stay\\b1\\fs{fs_en}\\c&HFFFFFF&\\bord{bord_cr}\\be{be_val}\\3c&H000000&"
                    f"\\pos({px},{py})}}{val}{{\\fnHayah\\fs{fs_ar}\\b0}} :{ar_label}"
                )
                lines.append(f"Dialogue: 3,{st},{en},Rights,,0,0,0,,{txt}")

    os.makedirs(os.path.dirname(os.path.abspath(out_ass_path)), exist_ok=True)
    with open(out_ass_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    return out_ass_path
