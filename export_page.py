"""Authenticated pod-local export page. ZIP verification precedes explicit deletion."""
import hashlib
import json
import os
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr

OUTPUT_ROOT = Path(os.environ.get('RUNPOD_OUTPUT_ROOT', '/workspace/outputs'))
EXPORT_ROOT = Path(os.environ.get('RUNPOD_EXPORT_ROOT', '/workspace/exports'))
_LOCK = threading.Lock()


def _log(message):
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    with (EXPORT_ROOT / 'export.log').open('a', encoding='utf-8') as file:
        file.write(datetime.now(timezone.utc).isoformat() + ' ' + message + '\n')


def _source(relative):
    path = OUTPUT_ROOT / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError('الملف مش موجود أو خارج مكتبة النتائج: ' + relative)
    if any(parent.is_symlink() for parent in path.parents if parent != OUTPUT_ROOT.parent):
        raise ValueError('مسارات الروابط الرمزية غير مسموحة.')
    if path.suffix.lower() != '.mp4' or any(tag in path.stem.lower() for tag in ('_preview', '.burned.', '.burning.', '.compressing.')):
        raise ValueError('اختار فيديو نهائي فقط: ' + relative)
    return path


def list_finished():
    rows = []
    if OUTPUT_ROOT.is_dir():
        for path in sorted(OUTPUT_ROOT.rglob('*.mp4')):
            relative = path.relative_to(OUTPUT_ROOT).as_posix()
            if any(part.startswith('_') for part in path.relative_to(OUTPUT_ROOT).parts[:-1]):
                continue
            try:
                path = _source(relative)
                if path.stat().st_size:
                    rows.append((f'{relative} ({path.stat().st_size / 1048576:.1f} MB)', relative))
            except (ValueError, OSError):
                continue
    return rows


def _fingerprint(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('الملف اتغير أثناء الفحص: ' + path.name)
    return {'size': after.st_size, 'mtime': after.st_mtime_ns, 'inode': after.st_ino, 'sha256': digest.hexdigest()}


def _verify_zip(path, manifest):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(manifest) or set(names) != set(manifest):
            raise ValueError('محتويات ZIP مش مطابقة للقائمة المختارة.')
        bad = archive.testzip()
        if bad:
            raise ValueError('ZIP تالف: ' + bad)
        for name, expected in manifest.items():
            if archive.getinfo(name).file_size != expected['size']:
                raise ValueError('حجم الملف داخل ZIP غير مطابق: ' + name)
            digest = hashlib.sha256()
            with archive.open(name) as file:
                for chunk in iter(lambda: file.read(4 * 1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected['sha256']:
                raise ValueError('بصمة الملف داخل ZIP غير مطابقة: ' + name)


def prepare_export(selected):
    with _LOCK:
        selected = list(dict.fromkeys(selected or []))
        if not selected:
            raise gr.Error('علّم على الملفات المطلوبة الأول.')
        allowed = {value for _, value in list_finished()}
        if not set(selected) <= allowed:
            raise gr.Error('القائمة اتغيرت. حدّثها واختار الملفات تاني.')
        EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        name = datetime.now().strftime('episodes-%Y%m%d-%H%M%S-') + token[:8]
        temporary = EXPORT_ROOT / (name + '.partial')
        final = EXPORT_ROOT / (name + '.zip')
        manifest = {}
        _log('PREPARE ' + token + ' ' + json.dumps(selected, ensure_ascii=False))
        try:
            with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for relative in selected:
                    path = _source(relative)
                    manifest[relative] = _fingerprint(path)
                    archive.write(path, arcname=relative)
                    if _fingerprint(path) != manifest[relative]:
                        raise ValueError('الملف اتغير أثناء إنشاء ZIP: ' + relative)
                    _log('ADDED ' + token + ' ' + relative)
            _verify_zip(temporary, manifest)
            os.replace(temporary, final)
            record = {'token': token, 'zip': final.name, 'files': manifest, 'deleted': []}
            (EXPORT_ROOT / (token + '.json')).write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            _log('VERIFIED ' + token + ' ' + final.name)
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            _log('FAILED originals retained ' + token + ' ' + str(exc))
            raise gr.Error('فشل تجهيز ZIP. الأصليات لسه موجودة. ' + str(exc))
        lines = ['ZIP اتعمل واتراجع CRC + SHA-256 والأحجام. الأصليات لسه موجودة.',
                 'بعد التأكيد هيتم حذف الملفات دي فقط:']
        lines.extend('- ' + relative for relative in selected)
        lines.append('ZIP محفوظ في: ' + str(final))
        return token, str(final), '\n'.join(lines), gr.update(visible=True), gr.update(value=False)


def delete_confirmed(token, confirmed):
    with _LOCK:
        if not confirmed or not token or not token.isalnum():
            raise gr.Error('راجع القائمة وعلّم على تأكيد الحذف الأول.')
        record_path = EXPORT_ROOT / (token + '.json')
        record = json.loads(record_path.read_text(encoding='utf-8'))
        archive = EXPORT_ROOT / record['zip']
        # Never trust an earlier verification after time has passed.
        _verify_zip(archive, record['files'])
        existing = []
        for relative, expected in record['files'].items():
            path = OUTPUT_ROOT / relative
            if not path.exists():
                _log('ALREADY_MISSING ' + token + ' ' + relative)
                continue
            path = _source(relative)
            if _fingerprint(path) != expected:
                _log('ABORT changed source ' + token + ' ' + relative)
                raise gr.Error('ملف اتغير من وقت تجهيز ZIP، مش هنمسح حاجة. جهّز ZIP جديد: ' + relative)
            existing.append((relative, path, expected))
        for relative, path, expected in existing:
            stat = path.stat()
            if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != (expected['inode'], expected['size'], expected['mtime']):
                raise gr.Error('توقف الحذف لأن الملف اتغير. ZIP محفوظ. راجع السجل.')
            try:
                path.unlink()
                record['deleted'].append(relative)
                record_path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
                _log('DELETED ' + token + ' ' + relative)
            except Exception as exc:
                _log('DELETE_STOPPED ' + token + ' ' + relative + ' ' + str(exc))
                raise gr.Error('توقف الحذف. ZIP سليم ومحفوظ. راجع السجل للملفات اللي اتمسحت: ' + str(exc))
        _log('COMPLETE ' + token + ' originals removed=' + str(len(existing)))
        return 'تم حذف الأصليات المختارة فقط. ZIP موجود في: ' + str(archive), gr.update(visible=False), gr.update(choices=list_finished(), value=[])


def build_export_ui():
    css = """
    body,.gradio-container{background:#081120!important}.gradio-container{max-width:1050px!important;margin:auto!important}
    .export-page{direction:rtl}.export-page p,.export-page div,.export-page a,.export-page textarea,.export-page h1,.export-page h2,.export-page span,.export-page label,.export-page strong{color:#e0ecff!important}
    .export-page .block,.export-page .form,.export-page .wrap,.export-page label,.export-page .file-preview,.export-page td{background:#101b2c!important}
    .export-page input,.export-page textarea{background:#090f1a!important;color:#e0ecff!important}.export-page textarea{direction:ltr;text-align:left}
    .export-page input[type=checkbox]{accent-color:#20bcf0;appearance:auto!important;opacity:1!important;cursor:pointer!important}
    .export-page label:has(input:checked){background:#143951!important;border-color:#43ceff!important}
    .export-page a{color:#65d9ff!important}.export-page button{font-size:16px!important;min-height:40px}
    .export-page code{background:#152944!important;color:#b6eeff!important}
    """
    with gr.Blocks(title='تصدير الحلقات') as page:
        gr.HTML('<style>' + css + '</style>')
        with gr.Column(elem_classes=['export-page']):
            gr.Markdown('# تصدير الحلقات · ZIP\nاختار الملفات النهائية. هنجهز ZIP من غير إعادة ضغط للفيديو، ونتأكد منه قبل أي حذف. ملفات المعاينة والملفات المؤقتة مش بتظهر.')
            gr.Markdown('مكان ملفات ZIP على البود: `' + str(EXPORT_ROOT) + '`\nافتح نفس المكان من اختيار الملفات في Chrome على 6080 عشان ترفع ZIP.')
            refresh = gr.Button('حدّث الملفات')
            selected = gr.CheckboxGroup(choices=list_finished(), label='الملفات النهائية الموجودة في outputs', interactive=True)
            prepare = gr.Button('١ · جهّز ZIP وافحصه', variant='primary')
            token = gr.State('')
            result = gr.File(label='تنزيل ZIP الجاهز', interactive=False)
            detail = gr.Textbox(label='نتيجة الفحص وقائمة الحذف بالضبط', lines=8, interactive=False)
            with gr.Column(visible=False) as confirmation:
                check = gr.Checkbox(label='راجعت القائمة وأوافق على حذف الأصليات المذكورة فقط بعد فحص ZIP', value=False, interactive=True)
                delete = gr.Button('٢ · تأكيد حذف الأصليات المختارة', variant='stop')
            gr.Markdown('السجل محفوظ في `' + str(EXPORT_ROOT / 'export.log') + '`. ZIP يفضل محفوظ حتى لو الحذف توقف. مفيش رفع سحابي تلقائي.')
            refresh.click(lambda: gr.update(choices=list_finished(), value=[]), outputs=selected)
            prepare.click(prepare_export, [selected], [token, result, detail, confirmation, check])
            delete.click(delete_confirmed, [token, check], [detail, confirmation, selected])
            selected.change(lambda: ('', gr.update(visible=False), False), outputs=[token, confirmation, check], queue=False)
    return page


def start_export_server(auth=None):
    if not auth:
        raise RuntimeError("Export page requires UI_USER and UI_PASS, same login as the main app.")
    page = build_export_ui()
    page.queue().launch(server_name='0.0.0.0', server_port=int(os.environ.get('EXPORT_PORT', '5050')), auth=auth,
                        prevent_thread_lock=True, allowed_paths=[str(EXPORT_ROOT)])
    return page
