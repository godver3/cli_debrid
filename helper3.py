import libtorrent as lt
import time

# Replace this with your actual magnet link
magnet_uri = "magnet:?xt=urn:btih:384c1df8f0ad94d6c1b990374a7eef159dac44dd"

# Create a session and set download parameters
session = lt.session()
params = lt.add_torrent_params()
params.save_path = "./"  # Specify the path where you want to save the files
params.url = magnet_uri

# Add the torrent using the magnet link
handle = session.add_torrent(params)

print("Downloading metadata...")

# Wait until metadata is downloaded
while not handle.status().has_metadata:
    time.sleep(1)

print("Metadata downloaded. Here is the list of files in the torrent:")

# Get the torrent info and list the files
torrent_info = handle.get_torrent_info()
for file in torrent_info.files():
    print(file.path, file.size)

print("Total files:", len(torrent_info.files()))
