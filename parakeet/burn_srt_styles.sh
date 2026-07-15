#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  burn_srt_styles.sh INPUT.mp4 SUBTITLES.srt [options]

Options:
  -o, --output-dir DIR   Output directory (default: ./output)
  -s, --font-size SIZE   Subtitle font size (default: 22)
      --style 1-10|all   Render one preset or all presets (default: all)
  -h, --help             Show this help
EOF
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
  usage
  exit 0
fi

if [[ $# -lt 2 ]]; then
  usage >&2
  exit 2
fi

input=$1
subtitles=$2
shift 2

output_dir=./output
font_size=22
selected_style=all

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output-dir)
      output_dir=${2:?Missing output directory}
      shift 2
      ;;
    -s|--font-size)
      font_size=${2:?Missing font size}
      shift 2
      ;;
    --style)
      selected_style=${2:?Missing style number}
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -f "$input" ]] || { echo "Input video not found: $input" >&2; exit 1; }
[[ -f "$subtitles" ]] || { echo "SRT file not found: $subtitles" >&2; exit 1; }
[[ "$font_size" =~ ^[0-9]+([.][0-9]+)?$ ]] ||
  { echo "Font size must be a positive number" >&2; exit 2; }
[[ "$font_size" != 0 && "$font_size" != 0.0 ]] ||
  { echo "Font size must be greater than zero" >&2; exit 2; }
[[ "$selected_style" == all || "$selected_style" =~ ^([1-9]|10)$ ]] ||
  { echo "Style must be 'all' or a number from 1 to 10" >&2; exit 2; }
command -v ffmpeg >/dev/null || { echo "ffmpeg is not installed" >&2; exit 1; }
ffmpeg_filters=$(ffmpeg -hide_banner -filters 2>/dev/null)
grep -qE '[[:space:]]subtitles[[:space:]]' <<<"$ffmpeg_filters" ||
  { echo "This ffmpeg build does not include the subtitles filter (libass)." >&2; exit 1; }

mkdir -p "$output_dir"

# Escape characters with special meaning inside an FFmpeg subtitles filter path.
subtitle_path=$(cd "$(dirname "$subtitles")" && pwd)/$(basename "$subtitles")
subtitle_path=${subtitle_path//\\/\\\\}
subtitle_path=${subtitle_path//:/\\:}
subtitle_path=${subtitle_path//\'/\\\'}
subtitle_path=${subtitle_path//,/\\,}
subtitle_path=${subtitle_path//[/\\[}
subtitle_path=${subtitle_path//]/\\]}

base=$(basename "$input")
base=${base%.*}

preset() {
  case "$1" in
    1)  echo "clean_white|FontName=Helvetica,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=0" ;;
    2)  echo "yellow_pop|FontName=Arial,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,Outline=3,Shadow=0" ;;
    3)  echo "cyan_modern|FontName=Avenir,Bold=1,PrimaryColour=&H00FFFF00,OutlineColour=&H00000000,Outline=2,Shadow=1" ;;
    4)  echo "green_neon|FontName=Futura,Bold=1,PrimaryColour=&H0000FF00,OutlineColour=&H00000000,Outline=3,Shadow=0" ;;
    5)  echo "pink_social|FontName=Verdana,Bold=1,PrimaryColour=&H00FF66FF,OutlineColour=&H00000000,Outline=3,Shadow=1" ;;
    6)  echo "orange_warm|FontName=Trebuchet MS,Bold=1,PrimaryColour=&H000080FF,OutlineColour=&H00000000,Outline=2,Shadow=1" ;;
    7)  echo "blue_box|FontName=Arial,Bold=1,PrimaryColour=&H00FFFFFF,BackColour=&H00CC5500,BorderStyle=3,Outline=1,Shadow=0" ;;
    8)  echo "black_box|FontName=Helvetica,Bold=1,PrimaryColour=&H00FFFFFF,BackColour=&H90000000,BorderStyle=3,Outline=1,Shadow=0" ;;
    9)  echo "classic_serif|FontName=Georgia,Bold=0,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1" ;;
    10) echo "mono_terminal|FontName=Menlo,Bold=1,PrimaryColour=&H0040FF40,OutlineColour=&H00000000,Outline=2,Shadow=0" ;;
  esac
}

render() {
  number=$1
  value=$(preset "$number")
  name=${value%%|*}
  style=${value#*|}
  output="$output_dir/${base}_${number}_${name}.mp4"

  echo "Rendering style $number: $name -> $output"
  ffmpeg -hide_banner -y \
    -i "$input" \
    -vf "subtitles=filename='$subtitle_path':force_style='$style,FontSize=$font_size,Alignment=2,MarginV=60'" \
    -c:v libx264 -crf 18 -preset medium \
    -c:a copy \
    "$output"
}

if [[ "$selected_style" == all ]]; then
  for number in 1 2 3 4 5 6 7 8 9 10; do
    render "$number"
  done
else
  render "$selected_style"
fi
