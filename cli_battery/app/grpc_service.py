import grpc
from concurrent import futures
import metadata_service_pb2
import metadata_service_pb2_grpc
from app.direct_api import DirectAPI
from app.logger_config import logger
import json
import datetime

class MetadataServicer(metadata_service_pb2_grpc.MetadataServiceServicer):
    def GetMovieMetadata(self, request, context):
        metadata, source = DirectAPI.get_movie_metadata(request.imdb_id)
        
        # Get release dates
        release_dates, release_dates_source = DirectAPI.get_movie_release_dates(request.imdb_id)
        
        # Add release dates to metadata
        metadata['release_dates'] = release_dates
        
        # Convert all values in metadata to strings
        string_metadata = {}
        for k, v in metadata.items():
            if isinstance(v, (list, dict)):
                string_metadata[k] = json.dumps(v)
            else:
                string_metadata[k] = str(v)
        
        return metadata_service_pb2.MetadataResponse(
            metadata=string_metadata,
            source=f"{source}, release dates: {release_dates_source}"
        )

    def GetMovieReleaseDates(self, request, context):
        release_dates, source = DirectAPI.get_movie_release_dates(request.imdb_id)
        if release_dates is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Release dates not found for IMDB ID: {request.imdb_id}")
            return metadata_service_pb2.ReleaseDatesResponse()
        
        return metadata_service_pb2.ReleaseDatesResponse(
            release_dates=json.dumps(release_dates),
            source=source
        )

    def GetEpisodeMetadata(self, request, context):
        try:
            metadata, source = DirectAPI.get_episode_metadata(request.imdb_id)
            
            if metadata is None:
                return metadata_service_pb2.MetadataResponse(
                    metadata={},
                    source="No data available"
                )
            
            # Convert all values in metadata to strings
            string_metadata = {}
            for k, v in metadata.items():
                if isinstance(v, (dict, list)):
                    string_metadata[k] = json.dumps(v)
                else:
                    string_metadata[k] = str(v)
            
            return metadata_service_pb2.MetadataResponse(
                metadata=string_metadata,
                source=source
            )
        except Exception as e:
            logger.exception("Error in GetEpisodeMetadata")
            context.abort(grpc.StatusCode.INTERNAL, f"Internal error: {str(e)}")

    def GetShowMetadata(self, request, context):
        metadata, source = DirectAPI.get_show_metadata(request.imdb_id)
        if metadata is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Show metadata not found for IMDb ID: {request.imdb_id}")
            return metadata_service_pb2.MetadataResponse()
        
        # Get seasons data
        seasons_data, seasons_source = DirectAPI.get_show_seasons(request.imdb_id)
        
        # Add seasons data to metadata
        metadata['seasons'] = seasons_data
        
        processed_metadata = {}
        for key, value in metadata.items():
            if isinstance(value, (dict, list)):
                processed_metadata[key] = json.dumps(value, default=self._json_serial)
            elif isinstance(value, datetime.datetime):
                processed_metadata[key] = value.isoformat()
            elif not isinstance(value, str):
                processed_metadata[key] = json.dumps(value, default=self._json_serial)
            else:
                processed_metadata[key] = value
        
        combined_source = f"{source}, seasons: {seasons_source}"
        return metadata_service_pb2.MetadataResponse(metadata=processed_metadata, source=combined_source)

    @staticmethod
    def _json_serial(obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")

    def GetShowSeasons(self, request, context):
        imdb_id = request.imdb_id
        logger.info(f"GetShowSeasons called for IMDb ID: {imdb_id}")
        
        try:
            seasons_data, source = DirectAPI.get_show_seasons(imdb_id)
            logger.info(f"Received seasons_data from {source} for IMDb ID: {imdb_id}")
            
            if not seasons_data:
                logger.warning(f"No seasons data found for IMDb ID: {imdb_id}")
                return metadata_service_pb2.ShowSeasonsResponse(seasons=[], source="No data available")
            
            seasons_list = []
            for season_number, season_info in seasons_data.items():
                try:
                    episode_count = season_info['episode_count']
                    season = metadata_service_pb2.Season(
                        season_number=int(season_number),
                        episode_count=episode_count
                    )
                    seasons_list.append(season)
                except (KeyError, ValueError) as e:
                    logger.error(f"Error processing season data for IMDb ID {imdb_id}, season {season_number}: {str(e)}")
                    continue
            
            # Sort seasons by season number
            seasons_list.sort(key=lambda x: x.season_number)
            
            logger.info(f"Returning {len(seasons_list)} seasons for IMDb ID: {imdb_id}")
            return metadata_service_pb2.ShowSeasonsResponse(seasons=seasons_list, source=source or "Unknown")
        
        except Exception as e:
            logger.exception(f"Unexpected error in GetShowSeasons for IMDb ID {imdb_id}: {str(e)}")
            context.abort(grpc.StatusCode.INTERNAL, f"Internal error: {str(e)}")

    def TMDbToIMDb(self, request, context):
        imdb_id, source = DirectAPI.tmdb_to_imdb(request.tmdb_id)
        return metadata_service_pb2.IMDbResponse(imdb_id=imdb_id, source=source)

    @staticmethod
    def _json_serial(obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    metadata_service_pb2_grpc.add_MetadataServiceServicer_to_server(MetadataServicer(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    server.wait_for_termination()

if __name__ == '__main__':
    serve()