#!/usr/bin/env bash
# Останавливаем скрипт при ошибках
set -o errexit

# Устанавливаем библиотеки Python
pip install -r requirements.txt

# Скачиваем статический FFmpeg для Linux сервера
wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar -xf ffmpeg-release-amd64-static.tar.xz
mv ffmpeg-*-amd64-static/ffmpeg .
mv ffmpeg-*-amd64-static/ffprobe .
rm -rf ffmpeg-*-amd64-static*