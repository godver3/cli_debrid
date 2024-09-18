import asyncio
import aiohttp
import math

async def get_plex_library_content():
    baseurl = 'http://localhost:32400'
    token = 'Wgw7vNa-nM25cYZxxyCK'

    headers = {
        'Accept': 'application/json',
    }

    # Adding 'includeGuids' parameter to ensure JSON response
    extra_params = {
        'includeGuids': '1',
    }

    async def fetch_section(session, section_id):
        params = {
            'X-Plex-Token': token,
            'X-Plex-Container-Start': '0',
            'X-Plex-Container-Size': '0',
            **extra_params,
        }

        url = f'{baseurl}/library/sections/{section_id}/all'

        # Get the total number of items in the section
        async with session.get(url, headers=headers, params=params) as response:
            if response.status != 200:
                print(f"Error fetching section {section_id}: {response.status}")
                text = await response.text()
                print(f"Response: {text}")
                return []
            try:
                data = await response.json()
            except aiohttp.ContentTypeError:
                text = await response.text()
                print(f"Non-JSON response: {text}")
                return []
            total_size = int(data['MediaContainer']['totalSize'])

        # Prepare to fetch all items in chunks concurrently
        chunk_size = 2000  # Adjust based on expected library size
        num_chunks = math.ceil(total_size / chunk_size)
        items = []

        sem = asyncio.Semaphore(10)  # Limit concurrent requests to avoid server overload

        async def fetch_chunk(start):
            async with sem:
                chunk_params = {
                    'X-Plex-Token': token,
                    'X-Plex-Container-Start': str(start),
                    'X-Plex-Container-Size': str(chunk_size),
                    **extra_params,
                }
                async with session.get(url, headers=headers, params=chunk_params) as response:
                    if response.status != 200:
                        print(f"Error fetching chunk starting at {start}: {response.status}")
                        text = await response.text()
                        print(f"Response: {text}")
                        return
                    try:
                        chunk_data = await response.json()
                        items_in_chunk = chunk_data['MediaContainer'].get('Metadata', [])
                        items.extend(items_in_chunk)
                    except aiohttp.ContentTypeError:
                        text = await response.text()
                        print(f"Non-JSON response in chunk starting at {start}: {text}")
                        return

        # Create and run tasks for all chunks
        tasks = [
            asyncio.create_task(fetch_chunk(i * chunk_size))
            for i in range(num_chunks)
        ]
        await asyncio.gather(*tasks)

        return items

    async def fetch_seasons_and_episodes(session, tv_shows):
        all_shows_data = []

        sem = asyncio.Semaphore(10)  # Limit concurrent requests

        async def fetch_show_details(show):
            async with sem:
                show_data = {
                    'title': show.get('title'),
                    'seasons': []
                }
                show_key = show.get('ratingKey')
                url = f'{baseurl}/library/metadata/{show_key}/children'
                params = {
                    'X-Plex-Token': token,
                    **extra_params,
                }
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status != 200:
                        print(f"Error fetching seasons for show '{show.get('title')}': {response.status}")
                        text = await response.text()
                        print(f"Response: {text}")
                        return
                    try:
                        seasons_data = await response.json()
                    except aiohttp.ContentTypeError:
                        text = await response.text()
                        print(f"Non-JSON response when fetching seasons for show '{show.get('title')}': {text}")
                        return
                    seasons = seasons_data['MediaContainer'].get('Metadata', [])
                    for season in seasons:
                        season_data = {
                            'title': season.get('title'),
                            'episodes': []
                        }
                        season_key = season.get('ratingKey')
                        season_url = f'{baseurl}/library/metadata/{season_key}/children'
                        async with session.get(season_url, headers=headers, params=params) as season_response:
                            if season_response.status != 200:
                                print(f"Error fetching episodes for season '{season.get('title')}' of show '{show.get('title')}': {season_response.status}")
                                text = await season_response.text()
                                print(f"Response: {text}")
                                continue
                            try:
                                episodes_data = await season_response.json()
                            except aiohttp.ContentTypeError:
                                text = await season_response.text()
                                print(f"Non-JSON response when fetching episodes for season '{season.get('title')}' of show '{show.get('title')}': {text}")
                                continue
                            episodes = episodes_data['MediaContainer'].get('Metadata', [])
                            for episode in episodes:
                                episode_data = {
                                    'title': episode.get('title'),
                                    'index': episode.get('index')
                                }
                                season_data['episodes'].append(episode_data)
                        show_data['seasons'].append(season_data)
                all_shows_data.append(show_data)

        tasks = [asyncio.create_task(fetch_show_details(show)) for show in tv_shows]
        await asyncio.gather(*tasks)

        return all_shows_data

    async with aiohttp.ClientSession() as session:
        # Fetch movies and TV shows concurrently
        movies_items_task = asyncio.create_task(fetch_section(session, 30))
        tv_shows_items_task = asyncio.create_task(fetch_section(session, 31))
        movies_items, tv_shows_items = await asyncio.gather(
            movies_items_task, tv_shows_items_task
        )

        # **Filter items by type to ensure correct processing**
        movies_items = [item for item in movies_items if item.get('type') == 'movie']
        tv_shows_items = [item for item in tv_shows_items if item.get('type') == 'show']

        # Now fetch seasons and episodes for TV shows
        tv_shows_data = await fetch_seasons_and_episodes(session, tv_shows_items)

    return movies_items, tv_shows_data

# To run the asynchronous function and get the results
def main():
    movies_items, tv_shows_data = asyncio.run(get_plex_library_content())
    return movies_items, tv_shows_data

# Example usage
if __name__ == "__main__":
    movies, tv_shows = main()
    print(f"Retrieved {len(movies)} movies and {len(tv_shows)} TV shows.")

    print("\nFirst 5 Movies:")
    for movie in movies[:5]:
        print(f"- {movie.get('title')}")

    print("\nFirst 5 TV Shows with Seasons and Episodes:")
    for show in tv_shows[:5]:
        print(f"\nShow: {show['title']}")
        for season in show['seasons']:
            print(f"  Season: {season['title']}")
            for episode in season['episodes']:
                print(f"    Episode {episode['index']}: {episode['title']}")
