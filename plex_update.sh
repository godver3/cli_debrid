#!/bin/bash

# Configuration
localip="192.168.1.51"
external="plexdomain.com"
plexip=$localip
plex_url="http://$plexip:32400"
token="2rJ8yc5bgSYFgeRxyU1S"
zurg_mount="/mnt/zurg"
webhook_url="http://cli-debrid.godver3.xyz/webhook/rclone"  # Using HTTP explicitly since we bypass HTTPS for webhooks

# First notify our webhook for each file
for arg in "$@"
do
    arg_clean=$(echo "$arg" | sed 's/\\//g')
    echo "Notifying webhook for: $arg_clean"
    encoded_webhook_arg=$(echo -n "$arg_clean" | python3 -c "import sys, urllib.parse as ul; print(ul.quote(sys.stdin.read()))")
    curl -s -X GET "$webhook_url?file=$encoded_webhook_arg" --noproxy "*"  # Added --noproxy to prevent any proxy interference
done

# Get Plex section IDs
section_ids=$(curl -sLX GET "$plex_url/library/sections" -H "X-Plex-Token: $token" | xmllint --xpath "//Directory/@key" - | grep -o 'key="[^"]*"' | awk -F'"' '{print $2}')
if [ -z "$section_ids" ]; then
    echo "Error: missing sections; the token seems to be broken"
    exit 1
fi
echo "Plex section IDs: $section_ids"
sleep 1

# Then do Plex updates
for arg in "$@"
do
    arg_clean=$(echo "$arg" | sed 's/\\//g')
    modified_arg="$zurg_mount/$arg_clean"
    echo "Updating in Plex: $modified_arg"
    encoded_arg=$(echo -n "$modified_arg" | python3 -c "import sys, urllib.parse as ul; print(ul.quote(sys.stdin.read()))")
    if [ -z "$encoded_arg" ]; then
        echo "Error: encoded argument is empty, check the input or encoding process"
        continue
    fi
    for section_id in $section_ids
    do
        final_url="${plex_url}/library/sections/${section_id}/refresh?path=${encoded_arg}&X-Plex-Token=${token}"
        curl -s "$final_url"
        echo "Triggered scan with URL: $final_url"
    done
done

echo "All updates completed!" 