import grpc
import metadata_service_pb2
import metadata_service_pb2_grpc
import json
import requests

# Assuming your Flask app is running on localhost:5000
FLASK_BASE_URL = "http://localhost:5001"

def parse_json_string(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

def print_nested_dict(data):
    def parse_value(v):
        if isinstance(v, str):
            return parse_json_string(v)
        return v

    parsed_data = {k: parse_value(v) for k, v in data.items()}
    print(json.dumps(parsed_data, indent=2, ensure_ascii=False))

def compare_grpc_with_flask():
    results = {}
    
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        
        # Movie Metadata (The Shawshank Redemption)
        movie_imdb_id = 'tt0111161'
        flask_response = requests.get(f"{FLASK_BASE_URL}/api/movie/metadata/{movie_imdb_id}")
        flask_movie_data = flask_response.json()
        grpc_movie_response = stub.GetMovieMetadata(metadata_service_pb2.IMDbRequest(imdb_id=movie_imdb_id))
        grpc_movie_metadata = {k: parse_json_string(v) for k, v in grpc_movie_response.metadata.items()}
        
        results['movie_metadata'] = {
            'match': flask_movie_data.get('data') == grpc_movie_metadata,
            'flask': flask_movie_data.get('data'),
            'grpc': grpc_movie_metadata
        }
        
        # Release Dates (The Shawshank Redemption)
        flask_response = requests.get(f"{FLASK_BASE_URL}/api/movie/release_dates/{movie_imdb_id}")
        flask_release_dates = flask_response.json()
        grpc_release_dates_response = stub.GetMovieReleaseDates(metadata_service_pb2.IMDbRequest(imdb_id=movie_imdb_id))
        grpc_release_dates = json.loads(grpc_release_dates_response.release_dates)
        
        results['release_dates'] = {
            'match': flask_release_dates == grpc_release_dates,
            'flask': flask_release_dates,
            'grpc': grpc_release_dates
        }
        
        # Show Metadata (Game of Thrones)
        show_imdb_id = 'tt0944947'
        flask_response = requests.get(f"{FLASK_BASE_URL}/api/show/metadata/{show_imdb_id}")
        flask_show_data = flask_response.json()
        grpc_show_response = stub.GetShowMetadata(metadata_service_pb2.IMDbRequest(imdb_id=show_imdb_id))
        grpc_show_metadata = {k: parse_json_string(v) for k, v in grpc_show_response.metadata.items()}
        
        results['show_metadata'] = {
            'match': flask_show_data.get('data') == grpc_show_metadata,
            'flask': flask_show_data.get('data'),
            'grpc': grpc_show_metadata
        }
        
        # Show Seasons (Game of Thrones)
        flask_response = requests.get(f"{FLASK_BASE_URL}/api/show/seasons/{show_imdb_id}")
        flask_seasons = flask_response.json()
        grpc_seasons_response = stub.GetShowSeasons(metadata_service_pb2.IMDbRequest(imdb_id=show_imdb_id))
        grpc_seasons = [{"season_number": s.season_number, "episode_count": s.episode_count} for s in grpc_seasons_response.seasons]
        
        results['show_seasons'] = {
            'match': flask_seasons == grpc_seasons,
            'flask': flask_seasons,
            'grpc': grpc_seasons
        }
        
        # Episode Metadata (Game of Thrones S01E01)
        episode_imdb_id = 'tt1480055'
        flask_response = requests.get(f"{FLASK_BASE_URL}/api/episode/metadata/{episode_imdb_id}")
        flask_episode_data = flask_response.json()
        grpc_episode_response = stub.GetEpisodeMetadata(metadata_service_pb2.IMDbRequest(imdb_id=episode_imdb_id))
        grpc_episode_metadata = {k: parse_json_string(v) for k, v in grpc_episode_response.metadata.items()}
        
        results['episode_metadata'] = {
            'match': flask_episode_data.get('data') == grpc_episode_metadata,
            'flask': flask_episode_data.get('data'),
            'grpc': grpc_episode_metadata
        }
        
        # TMDb to IMDb (Fight Club)
        tmdb_id = '550'
        flask_response = requests.get(f"{FLASK_BASE_URL}/api/tmdb_to_imdb/{tmdb_id}")
        flask_imdb_id = flask_response.json().get('imdb_id')
        grpc_tmdb_response = stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id=tmdb_id))
        
        results['tmdb_to_imdb'] = {
            'match': flask_imdb_id == grpc_tmdb_response.imdb_id,
            'flask': flask_imdb_id,
            'grpc': grpc_tmdb_response.imdb_id
        }
    
    return results

def run():
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        
        # Test GetMovieMetadata
        print("\n--- Testing GetMovieMetadata ---")
        movie_request = metadata_service_pb2.IMDbRequest(imdb_id='tt0111161')  # The Shawshank Redemption
        movie_response = stub.GetMovieMetadata(movie_request)
        print("GetMovieMetadata response:")
        print_nested_dict(dict(movie_response.metadata))
        print(f"Source: {movie_response.source}")
        input("Press Enter to continue...")

        # Test GetMovieReleaseDates
        print("\n--- Testing GetMovieReleaseDates ---")
        release_dates_request = metadata_service_pb2.IMDbRequest(imdb_id='tt0111161')  # The Shawshank Redemption
        release_dates_response = stub.GetMovieReleaseDates(release_dates_request)
        print("GetMovieReleaseDates response:")
        print(f"Source: {release_dates_response.source}")
        print("Release Dates:")
        print(json.dumps(json.loads(release_dates_response.release_dates), indent=2))
        print(f"Source: {release_dates_response.source}")
        input("Press Enter to continue...")

        # Test GetShowMetadata
        print("\n--- Testing GetShowMetadata ---")
        show_request = metadata_service_pb2.IMDbRequest(imdb_id='tt0944947')  # Game of Thrones
        show_response = stub.GetShowMetadata(show_request)
        print("GetShowMetadata response:")
        print_nested_dict(dict(show_response.metadata))
        print(f"Source: {show_response.source}")
        input("Press Enter to continue...")
        
        # Test GetEpisodeMetadata
        print("\n--- Testing GetEpisodeMetadata ---")
        episode_request = metadata_service_pb2.IMDbRequest(imdb_id='tt1480055')  # Game of Thrones S01E01
        episode_response = stub.GetEpisodeMetadata(episode_request)
        print("GetEpisodeMetadata response:")
        print_nested_dict(dict(episode_response.metadata))
        print(f"Source: {episode_response.source}")
        input("Press Enter to continue...")

        # Test GetShowSeasons
        print("\n--- Testing GetShowSeasons ---")
        seasons_request = metadata_service_pb2.IMDbRequest(imdb_id='tt0944947')  # Game of Thrones
        seasons_response = stub.GetShowSeasons(seasons_request)
        print("GetShowSeasons response:")
        seasons_data = [
            {
                "season_number": season.season_number,
                "episode_count": season.episode_count
            }
            for season in seasons_response.seasons
        ]
        print(json.dumps(seasons_data, indent=2))
        print(f"Source: {seasons_response.source}")
        input("Press Enter to continue...")

        # Test TMDbToIMDb
        print("\n--- Testing TMDbToIMDb ---")
        tmdb_request = metadata_service_pb2.TMDbRequest(tmdb_id='957452')  # Fight Club
        tmdb_response = stub.TMDbToIMDb(tmdb_request)
        print("TMDbToIMDb response:")
        print(json.dumps({"imdb_id": tmdb_response.imdb_id}, indent=2))
        print(f"Source: {tmdb_response.source}")
        input("Press Enter to continue...")

if __name__ == '__main__':
    run()