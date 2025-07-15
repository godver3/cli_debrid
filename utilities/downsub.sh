#!/bin/bash

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <video-file> <lang1> [<lang2> ...]"
  exit 1
fi

video="$1"
shift
langs="$*"

basename="${video%.*}"
temp_dir="/tmp/subliminal-sub-$$"

# Get the directory where this script is located
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_template="${script_dir}/config/subliminal.toml"
temp_config="${temp_dir}/subliminal.toml"

mkdir -p "$temp_dir"

# Get all credentials from settings as JSON
credentials_json=$(python3 "${script_dir}/get_subliminal_config.py")

# Parse JSON using python (more reliable than trying to parse in bash)
tmdb_api_key=$(echo "$credentials_json" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['tmdb_api_key'])")
opensubtitles_username=$(echo "$credentials_json" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['opensubtitles_username'])")
opensubtitles_password=$(echo "$credentials_json" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['opensubtitles_password'])")
omdb_api_key=$(echo "$credentials_json" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['omdb_api_key'])")

# Create comprehensive config file
cat > "$temp_config" << EOF
[default]
cache_dir = "~/.cache/subliminal"
EOF

# Add OpenSubtitles.com credentials if they exist
if [[ -n "$opensubtitles_username" && -n "$opensubtitles_password" ]]; then
    cat >> "$temp_config" << EOF

[provider.opensubtitlescom]
username = "$opensubtitles_username"
password = "$opensubtitles_password"
EOF
fi

# Add refiners
cat >> "$temp_config" << EOF

[refiner.omdb]
apikey = "$omdb_api_key"

[refiner.tmdb]
apikey = "$tmdb_api_key"

[download]
EOF

# Add providers based on whether we have OpenSubtitles.com credentials
if [[ -n "$opensubtitles_username" && -n "$opensubtitles_password" ]]; then
    cat >> "$temp_config" << EOF
provider = [
  "opensubtitlescom",  # Authenticated - higher priority
  "opensubtitles",     # Anonymous fallback
  "podnapisi",
  "bsplayer",
  "gestdown",
  "napiprojekt",
  "subtitulamos"
]
EOF
else
    cat >> "$temp_config" << EOF
provider = [
  "opensubtitles",     # Anonymous access
  "podnapisi",
  "bsplayer",
  "gestdown",
  "napiprojekt",
  "subtitulamos"
]
EOF
fi

# Add rest of config
cat >> "$temp_config" << EOF
refiner = ["metadata", "hash", "omdb", "tmdb"]
encoding = "utf-8"
min_score = 50
archives = true
verbose = 3
EOF

# Download to temp dir using the dynamic config file
subliminal --config "$temp_config" download --force -d "$temp_dir" $(for lang in $langs; do echo -n "-l $lang "; done) "$video"

# Move matching .srt back with symlink-style name(s)
for srt in "$temp_dir"/*.srt; do
  # Skip if no .srt files were found (glob didn't match)
  [[ -f "$srt" ]] || continue
  
  langcode=$(basename "$srt" | sed -E 's/.*\.([a-z]{2})\.srt$/\1/')
  if [[ "$langcode" != "$srt" ]]; then
    cp "$srt" "${basename}.${langcode}.srt"
    echo "Downloaded subtitle: ${basename}.${langcode}.srt"
  else
    cp "$srt" "${basename}.srt"
    echo "Downloaded subtitle: ${basename}.srt"
  fi
done

# Clean up
rm -rf "$temp_dir"
