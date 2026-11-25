#!/usr/bin/env bash
# First-time setup on a fresh pod, then starts the tool. Usage: bash setup.sh
set -e
cd "$(dirname "$0")"
apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y ffmpeg aria2 curl unzip fontconfig rclone p7zip-full xvfb fluxbox x11vnc novnc websockify wget
(apt-get install -y unrar || apt-get install -y rar || true)
if ! command -v google-chrome >/dev/null 2>&1; then wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && apt-get install -y /tmp/chrome.deb && rm -f /tmp/chrome.deb; fi
mkdir -p /workspace/models /workspace/glossaries /workspace/outputs /workspace/raw_episodes /root/.vnc
mkdir -p /usr/local/share/fonts/anime_subber && cp -f fonts/* /usr/local/share/fonts/anime_subber/ && fc-cache -f
ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html
VNCPASS_FILE=/workspace/.vnc_login
if [ ! -s "$VNCPASS_FILE" ]; then (umask 077; python3 -c "import secrets;print(secrets.token_urlsafe(6))" > "$VNCPASS_FILE"); fi
x11vnc -storepasswd "$(cat "$VNCPASS_FILE")" /root/.vnc/passwd >/dev/null
echo "Desktop (noVNC, port 6080) password: $(cat "$VNCPASS_FILE")  (saved in $VNCPASS_FILE)"
(nohup Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 < /dev/null &)
sleep 1
(DISPLAY=:99 nohup fluxbox >/dev/null 2>&1 < /dev/null &)
(nohup x11vnc -display :99 -rfbauth /root/.vnc/passwd -listen 127.0.0.1 -rfbport 5900 -forever -shared >/dev/null 2>&1 < /dev/null &)
(nohup websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 < /dev/null &)
(DISPLAY=:99 nohup google-chrome --no-sandbox --disable-dev-shm-usage --user-data-dir=/workspace/chrome_profile https://www.yourupload.com https://videa.hu >/dev/null 2>&1 < /dev/null &)
pip install -r requirements.txt
bash run.sh
