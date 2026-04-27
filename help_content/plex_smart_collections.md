# Plex Smart Collection Posters

Applies custom designed posters to the built-in smart collections Plex auto-generates — "Recently Added", "Top Rated", and similar library-level collections.

!!! warning "Posters only — no collection creation"
    This feature does **not** create smart collections. It only applies custom posters to smart collections Plex has already created automatically.

---

## Enable

Toggle **Enable Plex Smart Collection Posters**. Once enabled, all discovered smart collections appear as individual toggles below the design settings. Trigger the task manually after first enabling to populate the toggle list.

---

## Poster Design

One shared design applies to all enabled smart collections. Options: **Plex Default** (reverts to Plex's auto-generated poster) or **Layout 1–5** (same layouts as Plex Collections).

**Poster Accent Color** — Leave unset to use each layout's built-in default color. Click **Use default** to clear a custom color.

**Eyebrow Text** — Small text displayed above the collection title (e.g. `PLEX`, `SMART`). Leave blank to hide.

**Poster Icon** — Defaults to the CLI Debrid icon. Click **Choose icon from library** to select any overlay logo.

**Card Overlay Opacity** — Darkness of the gradient fade at the bottom of each card thumbnail. 0% = no fade, 100% = fully dark. Default 60%.

**Accent Glow Opacity** — Brightness of the accent color glow on the poster background. Default 80%.

**Accent Glow Radius** — How far the accent color spreads across the poster. Default 55. Increase to fill more of the poster with the accent color.

---

## Per-collection toggles

Each discovered Plex smart collection appears as an individual toggle. Only enabled collections receive a custom poster — disabled collections revert to Plex's default artwork.

---

## Schedule

Runs automatically every **24 hours**. Use the **Run Now** button or Task Manager to trigger immediately.

---

## State file

Content fingerprints are stored in `plex_smart_collection_state.json`. Clear it via **Debug → Manage Cache Files → Plex Smart Collection State** to force a full re-render on the next run.
