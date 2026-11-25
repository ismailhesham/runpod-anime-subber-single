# runpod-anime-subber-single — أداة الحلقة الواحدة

حلقة واحدة: رابط واحد، حرق 1080p وبعده نسخة 720p تتعمل منه، رفع، وزرار مسح بعد الرفع.

الريبو ده مستقل تماماً. مفيش أي ربط بينه وبين أداة تانية.

## أول مرة على بود جديد (في الـ Web Terminal)
```
cd /workspace && git clone https://github.com/ismailhesham/runpod-anime-subber-single.git && cd runpod-anime-subber-single && bash setup.sh
```

## تحديث وإعادة تشغيل
```
cd /workspace/runpod-anime-subber-single && git fetch origin && git reset --hard origin/main && bash run.sh
```

بعدها افتح الواجهة من RunPod: Connect ← HTTP Service (بورت 7860).

المفاتيح (DeepSeek / DoodStream) مش في الريبو: بتتحط من خانة الإعدادات في الواجهة وتتحفظ في `/workspace/subber_secrets.json`، أو من متغيرات البيئة `DEEPSEEK_API_KEY` و`DOODSTREAM_API_KEY`.
