'use strict';

const assert = require('node:assert/strict');
const {
    buildClassicSeasonResults,
    buildClassicEpisodeResults
} = require('../static/js/discover_addmedia.js');

const mediaData = {
    media_id: '1396',
    title: 'Card title',
    year: '2007',
    vote_average: 0,
    genre_ids: [],
    backdrop_path: '',
    overview: ''
};

const providerData = {
    title: 'Provider title',
    year: '2008',
    vote_average: 8.4,
    genres: ['Drama'],
    poster_path: '/show.jpg',
    seasons: [
        { season_number: 2, episode_count: 13 },
        { season_number: 0, episode_count: 4 },
        { season_number: 1, episode_count: 7, poster_path: '/season-1.jpg' },
        { season_number: 'invalid', episode_count: 1 }
    ]
};

const withoutSpecials = buildClassicSeasonResults(providerData, mediaData, false);
assert.deepEqual(withoutSpecials.map(item => item.season_num), [1, 2]);
assert.equal(withoutSpecials[0].title, 'Provider title');
assert.equal(withoutSpecials[0].poster_path, '/season-1.jpg');
assert.equal(withoutSpecials[1].poster_path, '/show.jpg');
assert.equal(withoutSpecials[1].episode_count, 13);
assert.equal(withoutSpecials[1].multi, true);

const withSpecials = buildClassicSeasonResults(providerData, { ...mediaData }, true);
assert.deepEqual(withSpecials.map(item => item.season_num), [0, 1, 2]);

const episodes = buildClassicEpisodeResults({
    episodes: [
        {
            episode_number: 1,
            name: 'Pilot',
            air_date: '2008-01-20',
            still_path: '/pilot.jpg',
            vote_average: 8.1
        },
        { episode_number: 2 }
    ]
}, {
    mediaId: '1396',
    title: 'Provider title',
    year: '2008',
    mediaType: 'tv',
    season: 1
});

assert.equal(episodes.length, 2);
assert.deepEqual(episodes[0], {
    id: '1396',
    title: 'Provider title',
    episode_title: 'Pilot',
    season_num: 1,
    episode_num: 1,
    year: '2008',
    media_type: 'tv',
    still_path: '/pilot.jpg',
    air_date: '2008-01-20',
    vote_average: 8.1,
    multi: false
});
assert.equal(episodes[1].episode_title, 'Episode 2');

console.log('Classic Add Media provider adapters passed');
