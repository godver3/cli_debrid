#!/bin/bash

# Define the shows directory
shows_directory="/mnt/zurg/shows"

# Define a regex pattern to match different season and episode formats
pattern='[Ss]?[0-9]{1,2}[XxEe][0-9]{1,2}|[Ee]pisode\ *[0-9]{1,2}|[Ee]p\.* *[0-9]{1,2}'

# Function to check if a file matches the updated pattern
matches_pattern() {
    local file=$1
    if [[ $file =~ $pattern ]]; then
        return 0
    else
        return 1
    fi
}

# Iterate through each show directory
for show in "$shows_directory"/*; do
    if [[ -d $show ]]; then
        # Find the first file that does not match the pattern
        for file in "$show"/*; do
            if [[ -f $file ]]; then
                if ! matches_pattern "$(basename "$file")"; then
                    echo "$file"
                    break
                fi
            fi
        done
    fi
done
