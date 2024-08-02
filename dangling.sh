#!/bin/bash

# Directory containing your Python files
DIR="."

# File containing the old function names
OLD_FUNCTIONS_FILE="old_functions.txt"

# Create a list of old function names
cat << EOF > $OLD_FUNCTIONS_FILE
similarity
calculate_bitrate
get_tmdb_season_info
get_media_info_for_bitrate
parse_size
preprocess_title
detect_season_pack
get_resolution_rank
extract_season_episode
extract_title_and_se
rank_result_key
scrape
EOF

# Function to search for references
search_references() {
    local func=$1
    local count=$(grep -R "\b$func\b" $DIR/*.py | wc -l)
    if [ $count -gt 0 ]; then
        echo "Function '$func' is still referenced $count times:"
        grep -Rn "\b$func\b" $DIR/*.py
        echo
    fi
}

# Main script
echo "Searching for references to old functions..."
echo

while read -r func; do
    search_references "$func"
done < "$OLD_FUNCTIONS_FILE"

echo "Search complete."
