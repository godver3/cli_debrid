#!/bin/bash

SCAN_DIR="${1:-.}"
MAX_FILES="${2:-0}"  # Second parameter for max files, 0 means no limit
echo "📁 Scanning: $SCAN_DIR"
if [[ $MAX_FILES -gt 0 ]]; then
    echo "🎯 Processing maximum $MAX_FILES files"
fi

shopt -s nullglob

processed_count=0

while read -r symlink; do
  # Check if we've reached the maximum number of files to process
  if [[ $MAX_FILES -gt 0 && $processed_count -ge $MAX_FILES ]]; then
    echo "🛑 Reached maximum of $MAX_FILES processed files. Stopping."
    break
  fi

  base="${symlink%.*}"
  srt_candidates=("${base}".*.srt)
  flag_file="${base}.subs-processed"

  if [[ -f "$flag_file" ]]; then
    echo "⏭️ Already processed (flag exists): $symlink"
    continue
  fi

  if [[ ${#srt_candidates[@]} -eq 0 ]]; then
    echo "➡️  Missing subtitle — downloading for: $symlink"

    # Check if we're in a virtual environment or need to activate one
    if [[ -z "$VIRTUAL_ENV" && -f "/root/myenv/bin/activate" ]]; then
      echo "🔄 Activating virtual environment at /root/myenv"
      source /root/myenv/bin/activate
    fi

    # Find the downsub.py script - check multiple locations
    if [[ -f "utilities/downsub.py" ]]; then
      DOWNSUB_PATH="utilities/downsub.py"
    elif [[ -f "/app/utilities/downsub.py" ]]; then
      DOWNSUB_PATH="/app/utilities/downsub.py"
    else
      echo "❌ Could not find downsub.py script"
      touch "$flag_file"  # Mark as processed to avoid infinite retries
      ((processed_count++))
      echo "📊 Progress: $processed_count processed (skipped - script not found)"
      continue
    fi

    # Run the subtitle downloader (removed input redirection to allow full output)
    echo "🐍 Using Python: $(which python3)"
    echo "📜 Using script: $DOWNSUB_PATH"
    python3 "$DOWNSUB_PATH" "$symlink"
    status=$?

    # Handle known errors
    if [[ $status -eq 0 ]]; then
      echo "✅ Subtitle download completed for: $symlink"
    else
      echo "⚠️  Subtitle download failed for: $symlink (exit code $status)"
    fi

    # Mark as processed regardless of success to avoid reprocessing
    touch "$flag_file"
    
    # Increment processed count since we actually took action
    ((processed_count++))
    echo "📊 Progress: $processed_count processed"
  else
    echo "✅ Subtitle already exists for: $symlink"
    # Mark as processed and increment counter since we took action (created flag)
    touch "$flag_file"
    ((processed_count++))
    echo "📊 Progress: $processed_count processed"
  fi
done < <(find "$SCAN_DIR" -type l \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" \))

echo "🏁 Processing complete. Total files actioned: $processed_count"
