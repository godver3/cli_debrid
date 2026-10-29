"""TMDB person search and filmography helpers for Discover."""

from urllib.parse import quote


def person_search_url(api_key, query, page=1):
    return (
        f'https://api.themoviedb.org/3/search/person'
        f'?api_key={api_key}&query={quote(query)}&page={page}&language=en-US'
        f'&include_adult=false'
    )


def person_details_url(api_key, person_id):
    return (
        f'https://api.themoviedb.org/3/person/{int(person_id)}'
        f'?api_key={api_key}&language=en-US&append_to_response=combined_credits,external_ids'
    )


def summarize_person(raw):
    """Compact person card from a TMDB search or details payload."""
    known = raw.get('known_for_department') or ''
    known_for = []
    for item in raw.get('known_for') or []:
        title = item.get('title') or item.get('name')
        if title:
            known_for.append(title)
        if len(known_for) >= 3:
            break
    return {
        'id': raw.get('id'),
        'name': raw.get('name') or '',
        'profile_path': raw.get('profile_path'),
        'known_for_department': known,
        'known_for': known_for,
        'popularity': raw.get('popularity') or 0,
    }


def _title_of(credit):
    return credit.get('title') or credit.get('name') or ''


def _date_of(credit):
    return credit.get('release_date') or credit.get('first_air_date') or ''


def _as_work(credit, roles):
    media_type = credit.get('media_type')
    if media_type not in ('movie', 'tv'):
        return None
    if credit.get('adult'):
        return None
    title = _title_of(credit)
    if not title:
        return None
    credit_id = credit.get('id')
    if not credit_id:
        return None
    return {
        'id': credit_id,
        'title': title,
        'name': title,
        'overview': credit.get('overview') or '',
        'poster_path': credit.get('poster_path'),
        'backdrop_path': credit.get('backdrop_path'),
        'release_date': _date_of(credit),
        'first_air_date': credit.get('first_air_date') or '',
        'vote_average': credit.get('vote_average') or 0,
        'vote_count': credit.get('vote_count') or 0,
        'popularity': credit.get('popularity') or 0,
        'genre_ids': credit.get('genre_ids') or [],
        'media_type': media_type,
        'character': credit.get('character') or '',
        'job': credit.get('job') or '',
        'roles': sorted(roles),
    }


def filmography(details, role='all'):
    """Unique movie/TV credits for a person, newest first.

    role: 'all', 'acting', or 'directing'. Directing is crew job Director.
    """
    credits = details.get('combined_credits') or {}
    merged = {}

    def add(raw, extra_roles):
        work = _as_work(raw, extra_roles)
        if not work:
            return
        key = (work['media_type'], work['id'])
        existing = merged.get(key)
        if existing is None:
            merged[key] = work
            return
        existing['roles'] = sorted(set(existing['roles']) | set(work['roles']))
        if work.get('character') and not existing.get('character'):
            existing['character'] = work['character']
        if work.get('job') and not existing.get('job'):
            existing['job'] = work['job']
        if (work.get('popularity') or 0) > (existing.get('popularity') or 0):
            existing['popularity'] = work['popularity']
            existing['poster_path'] = work.get('poster_path') or existing.get('poster_path')

    for item in credits.get('cast') or []:
        add(item, {'acting'})
    for item in credits.get('crew') or []:
        if item.get('job') == 'Director':
            add(item, {'directing'})

    wanted = None if role in (None, '', 'all') else role
    works = []
    for work in merged.values():
        if wanted and wanted not in work['roles']:
            continue
        works.append(work)
    works.sort(key=lambda w: (w.get('release_date') or '', w.get('popularity') or 0), reverse=True)
    return works


def person_payload(details, role='all'):
    person = summarize_person(details)
    person['biography'] = details.get('biography') or ''
    person['place_of_birth'] = details.get('place_of_birth') or ''
    person['birthday'] = details.get('birthday') or ''
    person['imdb_id'] = (details.get('external_ids') or {}).get('imdb_id') or details.get('imdb_id')
    all_works = filmography(details, 'all')
    if role == 'acting':
        works = [w for w in all_works if 'acting' in w['roles']]
    elif role == 'directing':
        works = [w for w in all_works if 'directing' in w['roles']]
    else:
        works = all_works
    return {
        'person': person,
        'results': works,
        'counts': {
            'all': len(all_works),
            'acting': sum(1 for w in all_works if 'acting' in w['roles']),
            'directing': sum(1 for w in all_works if 'directing' in w['roles']),
        },
    }
