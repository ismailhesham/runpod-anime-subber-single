#!/usr/bin/env bash
# Start the single tool on port 7860 (stops an older copy of itself first).
cd "$(dirname "$0")"
pkill -f "python3 $PWD/app.py" 2>/dev/null || true
sleep 1
# باسورد الواجهة: بيتعمل مرة واحدة ويتحفظ هنا (مش في الريبو)
LOGIN=/workspace/.ui_login
if [ ! -s "$LOGIN" ]; then (umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(12))" > "$LOGIN"); fi
UI_PASS_VALUE=$(cat "$LOGIN")
(UI_USER=admin UI_PASS="$UI_PASS_VALUE" GRADIO_SHARE=false PORT=7860 RUNPOD_OUTPUT_ROOT=/workspace/outputs nohup python3 "$PWD/app.py" > /workspace/app.log 2>&1 < /dev/null &)
sleep 8
tail -n 5 /workspace/app.log
echo "UI login: user=admin  password=$UI_PASS_VALUE  (محفوظ في $LOGIN)"
echo "READY (single) - open RunPod Connect > HTTP Service on port 7860"
