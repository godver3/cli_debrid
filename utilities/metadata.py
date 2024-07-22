def get_tv_show_details(overseerr_url, overseerr_api_key, tmdb_id):
    headers = get_overseerr_headers(overseerr_api_key)
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}?language=en")
    
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return {
            'numberOfSeasons': data.get('numberOfSeasons', 0),
            'seasons': data.get('seasons', [])
        }
    except requests.RequestException as e:
        logger.error(f"Error fetching TV show details for TMDb ID {tmdb_id}: {e}")
        return {'numberOfSeasons': 0, 'seasons': []}

def get_available_seasons(overseerr_url, overseerr_api_key, tmdb_id):
    headers = get_overseerr_headers(overseerr_api_key)
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}")
    
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return [season['seasonNumber'] for season in data.get('seasons', []) if not season.get('status', 0) == 3]
    except requests.RequestException as e:
        logger.error(f"Error fetching seasons for TV show with TMDb ID {tmdb_id}: {e}")
        return []

def get_next_season_to_request(available_seasons):
    if not available_seasons:
        return 1  # If no seasons are available, request the first season
    return max(available_seasons) + 1


def get_available_seasons(overseerr_url, overseerr_api_key, tmdb_id):
    headers = get_overseerr_headers(overseerr_api_key)
    url = get_url(overseerr_url, f"/api/v1/tv/{tmdb_id}")
    
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return [season['seasonNumber'] for season in data.get('seasons', []) if not season.get('status', 0) == 3]
    except requests.RequestException as e:
        logger.error(f"Error fetching seasons for TV show with TMDb ID {tmdb_id}: {e}")
        return []

def get_next_season_to_request(available_seasons):
    if not available_seasons:
        return 1  # If no seasons are available, request the first season
    return max(available_seasons) + 1

def add_to_overseerr(overseerr_url, overseerr_api_key, tmdb_id, media_type):
    headers = get_overseerr_headers(overseerr_api_key)
    url = get_url(overseerr_url, f"/api/v1/request")
    
    payload = {
        'mediaType': media_type,
        'mediaId': int(tmdb_id),
        'userId': 1,  # Adjust this if needed
        'is4k': False,
        'serverId': 0,
        'profileId': 0,
        'rootFolder': "",
        'languageProfileId': 0
    }

    if media_type == 'tv':
        available_seasons = get_available_seasons(overseerr_url, overseerr_api_key, tmdb_id)
        next_season = get_next_season_to_request(available_seasons)
        payload['seasons'] = [next_season]

    for attempt in range(MAX_RETRIES):
        try:
            logger.debug(f"Attempt {attempt + 1} - Payload for adding request: {payload}")
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            
            logger.debug(f"Raw response: {response.text}")
            
            if response.status_code == 200:
                logger.info(f"Successfully added {media_type} with TMDb ID {tmdb_id} to Overseerr.")
                return True, "Success"
            elif response.status_code == 400:
                response_json = response.json()
                message = response_json.get('message', 'Unknown error')
                if "already exists" in message.lower():
                    logger.info(f"{media_type.capitalize()} with TMDb ID {tmdb_id} already exists in Overseerr. Skipping.")
                    return True, "Already exists"
                else:
                    logger.warning(f"Bad request for {media_type} with TMDb ID {tmdb_id}: {message}")
                    return False, message
            elif response.status_code == 500:
                logger.warning(f"Attempt {attempt + 1} - Server error for {media_type} with TMDb ID {tmdb_id}. Retrying...")
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAY + random.uniform(0, JITTER)
                    time.sleep(delay)
                continue
            else:
                response.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Attempt {attempt + 1} - Error adding {media_type} with TMDb ID {tmdb_id} to Overseerr: {e}")
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY + random.uniform(0, JITTER)
                time.sleep(delay)
            continue
    
    return False, f"Failed after {MAX_RETRIES} attempts"

def fetch_all_overseerr_requests(overseerr_url, overseerr_api_key):
    headers = get_overseerr_headers(overseerr_api_key)
    all_requests = []
    page = 1
    take = 50  # Number of items to fetch per page

    while True:
        url = get_url(overseerr_url, f"/api/v1/request?take={take}&skip={take * (page - 1)}&filter=all&sort=added")

        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"Fetching Overseerr requests - Page {page}")
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                data = response.json()

                requests_batch = data.get('results', [])
                all_requests.extend(requests_batch)

                if len(requests_batch) < take:
                    # This is the last page
                    logger.info(f"Fetched a total of {len(all_requests)} requests from Overseerr")
                    return all_requests

                page += 1
                break  # Success, move to next page
            except requests.RequestException as e:
                logger.error(f"Attempt {attempt + 1} - Error fetching requests from Overseerr: {e}")
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAY + random.uniform(0, JITTER)
                    time.sleep(delay)
                else:
                    logger.error("Failed to fetch all requests after max retries")
                    return all_requests  # Return what we've managed to fetch so far

        time.sleep(random.uniform(0.5, 1.5))  # Add a small delay between pages

def sync_collected_to_overseerr():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logger.error("Overseerr URL or API key not set. Please configure in settings.")
        return

    collected_movies = get_all_media_items(state='Collected', media_type='movie')
    collected_episodes = get_all_media_items(state='Collected', media_type='episode')

    # Gather all requests from Overseerr
    all_requests = fetch_all_overseerr_requests(overseerr_url, overseerr_api_key)
    existing_tmdb_ids = {str(req['media']['tmdbId']) for req in all_requests}

    logger.debug(f"Current Overseerr TMDb IDs: {existing_tmdb_ids}")

    processed_tmdb_ids = set()
    failed_requests = []
    skipped_requests = []

    # Add missing movies to Overseerr
    for movie in collected_movies:
        tmdb_id = movie['tmdb_id'] if 'tmdb_id' in movie.keys() else None
        if not tmdb_id or tmdb_id == 'None':
            logger.warning(f"Skipping movie with invalid TMDb ID: {dict(movie)}")
            continue

        tmdb_id = str(tmdb_id)
        if tmdb_id in processed_tmdb_ids or tmdb_id in existing_tmdb_ids:
            continue

        logger.debug(f"Processing movie with TMDb ID {tmdb_id}")
        success, message = add_to_overseerr(overseerr_url, overseerr_api_key, tmdb_id, 'movie')
        if not success:
            failed_requests.append(('movie', tmdb_id, message))
        elif message != "Success":
            skipped_requests.append(('movie', tmdb_id, message))

        processed_tmdb_ids.add(tmdb_id)

    # Add missing TV shows to Overseerr
    tv_show_tmdb_ids = {str(episode['tmdb_id']) for episode in collected_episodes if 'tmdb_id' in episode.keys() and episode['tmdb_id'] and episode['tmdb_id'] != 'None'}
    for tmdb_id in tv_show_tmdb_ids:
        if tmdb_id in processed_tmdb_ids or tmdb_id in existing_tmdb_ids:
            continue

        logger.debug(f"Processing TV show with TMDb ID {tmdb_id}")
        success, message = add_to_overseerr(overseerr_url, overseerr_api_key, tmdb_id, 'tv')
        if not success:
            failed_requests.append(('tv', tmdb_id, message))
        elif message != "Success":
            skipped_requests.append(('tv', tmdb_id, message))

        processed_tmdb_ids.add(tmdb_id)

    if failed_requests:
        logger.warning("Failed to add the following requests to Overseerr:")
        for media_type, tmdb_id, reason in failed_requests:
            logger.warning(f"  {media_type.capitalize()} with TMDb ID {tmdb_id}: {reason}")

    if skipped_requests:
        logger.info("Skipped the following requests:")
        for media_type, tmdb_id, reason in skipped_requests:
            logger.info(f"  {media_type.capitalize()} with TMDb ID {tmdb_id}: {reason}")

    logger.info(f"Successfully processed {len(processed_tmdb_ids)} items.")
    logger.info(f"Failed requests: {len(failed_requests)}")
    logger.info(f"Skipped requests: {len(skipped_requests)}")