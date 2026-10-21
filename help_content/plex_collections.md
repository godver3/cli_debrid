# Plex Collections

Automatically create and maintain Plex collections that mirror your **Trakt Lists**, **MDBList**, or **Adaptive List** sources — in the correct sort order with a custom designed poster.

!!! note "Supported sources only"
    Plex Collections is only available for **Trakt Lists**, **MDBList**, and **Adaptive List** sources. Other source types do not support this feature.

!!! note "Plex only"
    Requires Plex as your media server. Hidden when using Jellyfin/Emby.

---

## Enable

Toggle **Enable Plex Collection** in the Plex Collection section of any supported content source.

---

## Collection Name

Sets the Plex collection title. Defaults to the source display name. For mixed (Movies + Shows) lists, " Movies" and " Shows" are appended automatically unless you set override names below.

---

## Sort Prefix

Prefix added to the sort title so the collection appears at the top of Plex's collection list. For example `!!!!` gives sort title `!!!!My List`. Leave empty to sort normally.

---

## Sort By / Sort Direction

Controls the order of items inside the collection. Options: **Default** (source list order), Title, Year, Release Date, Collected At, Runtime, **Random**.

**Random** reshuffles the collection order on every sync run.

---

## Poster Design

Choose a layout for the collection poster. Leave on **Plex Default** to use Plex's auto-generated artwork.

The first 4 collected items from your list are used as poster art inside the design. Changing the design, colour, eyebrow, or icon triggers an automatic re-render on the next source run.

**Poster Accent Color** — Leave unset to use each layout's default colour. Click **Use default** to clear a custom colour.

**Eyebrow Text** — Small text above the collection title (e.g. `TRAKT`, `THIS WEEK`). Leave blank to hide.

**Poster Icon** — Defaults to the source type icon. Click **Choose icon from library** to select any overlay logo. Wide logos are automatically scaled to fill the available space.

**Card Overlay Opacity** — Darkness of the gradient fade at the bottom of each card thumbnail. 0% = no fade, 100% = fully dark. Default 60%.

**Accent Glow Opacity** — Brightness of the accent color glow on the poster background. 0% = no glow, 100% = maximum brightness. Default 80%.

**Accent Glow Radius** — How far the accent color spreads across the poster. Default 55 (≈ 0.55 radius). Increase to fill more of the poster with the accent color.
