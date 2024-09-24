import grpc
import metadata_service_pb2
import metadata_service_pb2_grpc
import requests
import json
import time
import concurrent.futures
import psycopg2
from psycopg2.extras import RealDictCursor
import os

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

def compare_outputs(imdb_id):
    # gRPC request
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        grpc_request = metadata_service_pb2.IMDbRequest(imdb_id=imdb_id)
        grpc_response = stub.GetMovieMetadata(grpc_request)
        grpc_data = {k: parse_json_string(v) for k, v in grpc_response.metadata.items()}

    # Flask API request
    flask_response = requests.get(f"http://localhost:5001/api/movie/metadata/{imdb_id}")
    flask_data = flask_response.json().get('data', {})

    print("gRPC Output:")
    print_nested_dict(grpc_data)
    print("\nFlask API Output:")
    print_nested_dict(flask_data)

    # Compare outputs
    if grpc_data == flask_data:
        print("\nOutputs match!")
    else:
        print("\nOutputs do not match. Differences:")
        for key in set(grpc_data.keys()) | set(flask_data.keys()):
            if key not in grpc_data:
                print(f"Key '{key}' missing in gRPC output")
            elif key not in flask_data:
                print(f"Key '{key}' missing in Flask API output")
            elif grpc_data[key] != flask_data[key]:
                print(f"Mismatch for key '{key}':")
                print("gRPC:", grpc_data[key])
                print("Flask:", flask_data[key])

def fetch_grpc(imdb_id):
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        grpc_request = metadata_service_pb2.IMDbRequest(imdb_id=imdb_id)
        return stub.GetMovieMetadata(grpc_request)

def fetch_flask(imdb_id):
    return requests.get(f"http://localhost:5001/api/movie/metadata/{imdb_id}")

def fetch_postgres(imdb_id):
    conn = psycopg2.connect(
        dbname=os.environ.get('DB_NAME', 'cli_battery_database'),
        user=os.environ.get('DB_USER', 'cli_debrid'),
        password=os.environ.get('DB_PASSWORD', 'cli_debrid'),
        host=os.environ.get('DB_HOST', '192.168.1.51'),
        port=os.environ.get('DB_PORT', '5433')
    )
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT i.*, m.key, m.value
            FROM items i
            LEFT JOIN metadata m ON i.id = m.item_id
            WHERE i.imdb_id = %s
        """, (imdb_id,))
        result = cur.fetchall()
    conn.close()
    return result

def stress_test(num_requests=500):
    imdb_ids = ["tt16366836"] * num_requests  # You can add more IDs if needed

    # gRPC stress test
    grpc_start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        grpc_results = list(executor.map(fetch_grpc, imdb_ids))
    grpc_end_time = time.time()
    grpc_total_time = grpc_end_time - grpc_start_time

    # Flask API stress test
    flask_start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        flask_results = list(executor.map(fetch_flask, imdb_ids))
    flask_end_time = time.time()
    flask_total_time = flask_end_time - flask_start_time

    # PostgreSQL direct query stress test
    postgres_start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        postgres_results = list(executor.map(fetch_postgres, imdb_ids))
    postgres_end_time = time.time()
    postgres_total_time = postgres_end_time - postgres_start_time

    print(f"\nStress Test Results ({num_requests} requests):")
    print(f"gRPC Total Time: {grpc_total_time:.2f} seconds")
    print(f"gRPC Average Time per Request: {grpc_total_time/num_requests:.4f} seconds")
    print(f"Flask API Total Time: {flask_total_time:.2f} seconds")
    print(f"Flask API Average Time per Request: {flask_total_time/num_requests:.4f} seconds")
    print(f"PostgreSQL Total Time: {postgres_total_time:.2f} seconds")
    print(f"PostgreSQL Average Time per Request: {postgres_total_time/num_requests:.4f} seconds")

if __name__ == "__main__":
    imdb_id = "tt16366836"  # You can change this to any IMDb ID you want to test
    compare_outputs(imdb_id)
    stress_test()
