import logging
from .client import RealDebridProvider
from .api import make_request, get_api_key
import json

logging.basicConfig(level=logging.INFO)

def test_add_torrent_response():
    """Test the response from adding a torrent to Real-Debrid"""
    api_key = get_api_key()
    
    # Test with a known cached magnet
    magnet = "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Fexplodie.org%3A6969&tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969&tr=udp%3A%2F%2Ftracker.empire-js.us%3A1337&tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337&tr=wss%3A%2F%2Ftracker.btorrent.xyz&tr=wss%3A%2F%2Ftracker.fastcast.nz&tr=wss%3A%2F%2Ftracker.openwebtorrent.com&ws=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2F&xs=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2Fbig-buck-bunny.torrent"
    
    try:
        # Get raw response from add_torrent API call
        data = {'magnet': magnet}
        response = make_request('POST', '/torrents/addMagnet', api_key, data=data)
        print("\nAdd Torrent Response:")
        print(json.dumps(response, indent=2))
        
        # Get torrent info for comparison
        if 'id' in response:
            info = make_request('GET', f'/torrents/info/{response["id"]}', api_key)
            print("\nTorrent Info Response:")
            print(json.dumps(info, indent=2))
            
            # Clean up
            make_request('DELETE', f'/torrents/delete/{response["id"]}', api_key)
            
    except Exception as e:
        print(f"Error during test: {str(e)}")

if __name__ == '__main__':
    test_add_torrent_response()
