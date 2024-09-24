import grpc
import metadata_service_pb2
import metadata_service_pb2_grpc
import requests
import json

def print_nested_structure(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))

def compare_show_seasons(imdb_id):
    # gRPC request
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        grpc_request = metadata_service_pb2.IMDbRequest(imdb_id=imdb_id)
        grpc_response = stub.GetShowSeasons(grpc_request)
        grpc_data = {
            'seasons': {
                season_number: {
                    'episode_count': season_info.episode_count,
                    'episodes': {
                        ep_number: {
                            'first_aired': ep_info.first_aired,
                            'runtime': ep_info.runtime,
                            'title': ep_info.title
                        } for ep_number, ep_info in season_info.episodes.items()
                    }
                } for season_number, season_info in grpc_response.seasons.items()
            },
            'source': grpc_response.source
        }

    # Flask API request
    flask_response = requests.get(f"http://localhost:5001/api/show/seasons/{imdb_id}")
    flask_data = flask_response.json()

    print("gRPC Output:")
    print_nested_structure(grpc_data)
    print("\nFlask API Output:")
    print_nested_structure(flask_data)

    # Compare outputs
    if grpc_data == flask_data:
        print("\nOutputs match!")
    else:
        print("\nOutputs do not match. Differences:")
        
        # Compare seasons
        grpc_seasons = grpc_data['seasons']
        flask_seasons = flask_data[0] if isinstance(flask_data, list) and len(flask_data) > 0 else {}
        
        for season_number in set(grpc_seasons.keys()) | set(flask_seasons.keys()):
            if season_number not in grpc_seasons:
                print(f"Season {season_number} missing in gRPC output")
            elif season_number not in flask_seasons:
                print(f"Season {season_number} missing in Flask output")
            else:
                grpc_season = grpc_seasons[season_number]
                flask_season = flask_seasons[season_number]
                
                if grpc_season['episode_count'] != flask_season['episode_count']:
                    print(f"Mismatch in episode count for season {season_number}:")
                    print(f"  gRPC: {grpc_season['episode_count']}")
                    print(f"  Flask: {flask_season['episode_count']}")
                
                # Compare episodes
                grpc_episodes = grpc_season['episodes']
                flask_episodes = flask_season['episodes']
                
                for episode_number in set(grpc_episodes.keys()) | set(flask_episodes.keys()):
                    if episode_number not in grpc_episodes:
                        print(f"Episode {episode_number} of season {season_number} missing in gRPC output")
                    elif episode_number not in flask_episodes:
                        print(f"Episode {episode_number} of season {season_number} missing in Flask output")
                    else:
                        grpc_episode = grpc_episodes[episode_number]
                        flask_episode = flask_episodes[episode_number]
                        
                        for key in ['first_aired', 'runtime', 'title']:
                            if grpc_episode[key] != flask_episode[key]:
                                print(f"Mismatch in {key} for season {season_number}, episode {episode_number}:")
                                print(f"  gRPC: {grpc_episode[key]}")
                                print(f"  Flask: {flask_episode[key]}")

        # Compare sources
        if grpc_data['source'] != flask_data[1]:
            print("Mismatch in source:")
            print(f"  gRPC: {grpc_data['source']}")
            print(f"  Flask: {flask_data[1]}")

if __name__ == "__main__":
    imdb_id = "tt0944947"  # Game of Thrones
    compare_show_seasons(imdb_id)