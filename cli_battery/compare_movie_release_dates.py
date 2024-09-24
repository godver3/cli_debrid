import grpc
import metadata_service_pb2
import metadata_service_pb2_grpc
import requests
import json

def parse_json_string(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

def print_nested_structure(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))

def compare_release_dates(imdb_id):
    # gRPC request
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        grpc_request = metadata_service_pb2.IMDbRequest(imdb_id=imdb_id)
        grpc_response = stub.GetMovieReleaseDates(grpc_request)
        grpc_data = {
            'release_dates': parse_json_string(grpc_response.release_dates),
            'source': grpc_response.source
        }

    # Flask API request
    flask_response = requests.get(f"http://localhost:5001/api/movie/release_dates/{imdb_id}")
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
        
        # Handle both list and dictionary structures
        if isinstance(flask_data, list):
            if len(flask_data) == 2 and isinstance(flask_data[0], dict) and isinstance(flask_data[1], str):
                flask_data = {'release_dates': flask_data[0], 'source': flask_data[1]}
            else:
                print("Unexpected structure in Flask API output")
                return

        for key in set(grpc_data.keys()) | set(flask_data.keys()):
            if key not in grpc_data:
                print(f"Key '{key}' missing in gRPC output")
            elif key not in flask_data:
                print(f"Key '{key}' missing in Flask API output")
            elif grpc_data[key] != flask_data[key]:
                print(f"Mismatch for key '{key}':")
                print("gRPC:", grpc_data[key])
                print("Flask:", flask_data[key])

if __name__ == "__main__":
    imdb_id = "tt0111161"  # The Shawshank Redemption
    compare_release_dates(imdb_id)
