#!/usr/bin/env bash
# The film at the top of shaliach.me. Swapping it for a new render is this one command:
#
#   scripts/site-hero.sh <wide 16:9 video> <tall 9:16 or 4:5 video> [seconds]
#
# It writes docs/site/media/hero-16x9.{mp4,jpg} and hero-9x16.{mp4,jpg} (the names
# docs/site/index.html plays; the tall one is shown on portrait phones), web-sized:
# H.264, faststart, 720p, ~2 MB each. `seconds` trims the end (e.g. to drop an end card);
# the last 0.6 s fades out. Commit the four files; the Pages build publishes them.
set -euo pipefail

[ $# -ge 2 ] || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
wide="$1" tall="$2" secs="${3:-}"
out="$(cd "$(dirname "$0")/.." && pwd)/docs/site/media"
mkdir -p "$out"

enc() { # <in> <out.mp4> <w:h box>
  local trim=() vf="scale=$3:force_original_aspect_ratio=decrease:flags=lanczos,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p" af="anull"
  if [ -n "$secs" ]; then
    local st; st="$(awk -v s="$secs" 'BEGIN{print s-0.6}')"
    trim=(-t "$secs"); vf="$vf,fade=t=out:st=$st:d=0.6"; af="afade=t=out:st=$st:d=0.6"
  fi
  ffmpeg -v error -y -i "$1" ${trim[@]+"${trim[@]}"} -vf "$vf" -af "$af" \
    -c:v libx264 -preset slow -crf 25 -profile:v high -maxrate 2200k -bufsize 4400k \
    -c:a aac -b:a 96k -ac 2 -movflags +faststart "$2"
  # The poster: a frame a third of the way in, where the product is on screen.
  local d; d="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$2")"
  ffmpeg -v error -y -ss "$(awk -v d="$d" 'BEGIN{print d/3}')" -i "$2" -frames:v 1 -q:v 4 "${2%.mp4}.jpg"
}

enc "$wide" "$out/hero-16x9.mp4" 1280:720
enc "$tall" "$out/hero-9x16.mp4" 720:1280
ls -l "$out"/hero-*
