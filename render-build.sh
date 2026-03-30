#!/usr/bin/env bash
# Exit on error
set -o errexit

pip install -r requirements.txt

# Install FFmpeg for Render
mkdir -p bin
curl -L https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz | tar -xJ --strip-components=2 -C bin/ ffmpeg-master-latest-linux64-gpl/bin/ffmpeg
