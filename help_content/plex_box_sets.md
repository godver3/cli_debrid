# Plex Movie Box Sets

Automatically discovers TMDB franchise memberships for your movies, creates matching Plex collections, applies franchise posters, and optionally queues missing franchise movies.

!!! note "Movies only"
    Box sets are built from TMDB franchise/collection data which covers movies only.

---

## Enable

Toggle **Enable Plex Movie Box Sets**. Requires Plex URL/token and a TMDB API key.

---

## Grab Missing Movies

When enabled, franchise movies not in your library are automatically added to the wanted queue using the configured version.

---

## Collection Name Pattern

Template for Plex collection names. `{title}` is replaced with the franchise name from TMDB.

Examples: `{title} Collection`, `{title} Box Set`, `{title} Saga`

Suffix words (Collection, Box Set, Saga, Series, Trilogy, Universe, Franchise) are automatically stripped from the TMDB name before your pattern is applied — so you never get "The Godfather Collection Collection".

---

## Minimum Owned Movies

Minimum number of owned movies required to create a collection. Collections that drop below this threshold are **automatically deleted from Plex** on the next run.

Default: **2**. Set to 1 if you want collections created as soon as you own a single movie from a franchise (combined with Grab Missing to complete the set).

---

## Collection Sort Order

How movies are ordered inside the Plex collection:

- **Release Date** — oldest to newest (default)
- **Title** — alphabetical
- **Custom** — manual order

---

## Schedule

Runs automatically every **24 hours**. Use the **Run Now** button or Task Manager to trigger immediately.

The first run performs a TMDB lookup for every movie in your database (~30 minutes for large libraries). Subsequent runs are near-instant as already-checked movies are skipped.

---

## State file

Poster fingerprints are stored in `plex_boxsets_state.json`. Clear it via **Debug → Manage Cache Files → Plex Box Sets State** to force a full poster re-apply on the next run.
