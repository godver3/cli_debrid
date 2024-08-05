{% extends "base.html" %}

{% block title %}Scraper{% endblock %}

{% block content %}
    <h2>Manual Scraper</h2>
    <form method="POST">
        <input type="text" name="search_term" placeholder="Enter search term" required>
        <button type="submit">Search</button>
    </form>

    {% if result %}
        <h3>Search Results</h3>
        <div class="result">
            <p><strong>Title:</strong> {{ result.title }}</p>
            <p><strong>Year:</strong> {{ result.year }}</p>
            <p><strong>IMDB ID:</strong> {{ result.imdb_id }}</p>
            <p><strong>TMDB ID:</strong> {{ result.tmdb_id }}</p>
            <p><strong>Type:</strong> {{ result.movie_or_episode }}</p>
            {% if result.movie_or_episode == 'episode' %}
                <p><strong>Season:</strong> {{ result.season }}</p>
                <p><strong>Episode:</strong> {{ result.episode }}</p>
            {% endif %}
            <p><strong>Multi:</strong> {{ result.multi }}</p>
        </div>
    {% endif %}
{% endblock %}