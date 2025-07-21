#!/bin/bash

SCAN_DIR="${1:-.}"
echo "📁 Scanning: $SCAN_DIR"

shopt -s nullglob

while read -r symlink; do
  base="${symlink%.*}"
  srt_candidates=("${base}".*.srt)
  flag_file="${base}.subs-processed"

  if [[ -f "$flag_file" ]]; then
    echo "⏭️ Already processed (flag exists): $symlink"
    continue
  fi

  if [[ ${#srt_candidates[@]} -eq 0 ]]; then
    echo "➡️  Missing subtitle — downloading for: $symlink"

    # Run the subtitle downloader
    docker exec -i cli_debrid python3 utilities/downsub.py "$symlink" </dev/null
    status=$?

    # Handle known errors
    if [[ $status -eq 0 ]]; then
      echo "✅ Subtitle download completed for: $symlink"
    else
      echo "⚠️  Subtitle download failed for: $symlink (exit code $status)"
    fi

    # Mark as processed regardless of success to avoid reprocessing
    touch "$flag_file"
  else
    echo "✅ Subtitle already exists for: $symlink"
  fi
done < <(find "$SCAN_DIR" -type l \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" \))
