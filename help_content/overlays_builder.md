### Layout Builder Help

The Layout Builder is a visual, canvas-based designer for creating overlay layouts. A layout defines exactly how badges are arranged on a poster — what information is displayed, where each element is positioned, what fonts and colors are used, and how the background tray (if any) is styled. Layouts are saved and then applied from the Overlay Management page.

**Understanding Layouts:**

A layout is a collection of badges placed on a 600×900 pixel reference canvas (standard movie poster ratio). When the overlay system applies a layout to a real poster, all badge positions and sizes are automatically scaled to match the actual poster dimensions. You design once at 600×900 and it renders correctly across all poster sizes.

Layouts are assigned a **media type** — Movies, TV Shows, Season Posters, or Both (Movies & TV Shows). The media type controls which dropdown each layout appears in on the Overlay Management page.

---

**Interface Overview:**

The builder is divided into three panels:

*   **Left Panel** — Badge palette and the list of badges added to the current layout.
*   **Center Panel** — The canvas with a live preview of your layout on a real poster image.
*   **Right Panel** — Properties editor for the currently selected badge or tray, plus layout metadata and preview data controls.

---

**Left Panel — Badge Palette:**

This is where you pick badge types to add to the canvas. Each type adds a badge with sensible defaults that you can then customize in the right panel.

*   **Tray** — A background panel that sits behind a group of badges. Use it as a semi-transparent strip or box — typically at the bottom of the poster — to give badges a unified visual foundation. Trays support solid or gradient fills, optional borders, border radius, and opacity. Add a tray first, then position badges on top of it. A tray has no text or icon of its own — it is purely a background element. Configured via the **Tray Properties** panel when selected.

*   **Text Badges** — The most versatile badge type. Renders text directly onto the poster, driven by a `{{variable}}` template (e.g., `{{imdbRating}}`, `{{resolution}}`, `{{audioChannels}}`). Supports a background box, an optional icon/logo to the left or right of the text, full font control (family, size, weight, color, alignment), and optional vertical stacking for two-line labels. Presets are available for every common data type (ratings, resolution, HDR, audio, video codec, network, studio, content rating, show status, versions) plus a free-form Custom type for any combination of text and variables. Use Text Badges when you want precise control over appearance or need to display data that Smart Badges do not cover. Configured via the **Badge Properties** panel when selected.

*   **Smart Badges / Library Badges** — Image-based badges auto-selected at render time from your Badge Library. Instead of rendering text, the system looks up the matching PNG file based on the item's metadata (e.g., it selects `audio-truehd-atmos.png` for a TrueHD Atmos track). You control the badge's size and position in the layout; the correct image is chosen automatically. Requires PNG assets to be uploaded in the Badge Library. If no matching asset exists for a given item, the badge is silently skipped — no error, no empty space. Configured via the **Smart Badge Properties** panel when selected (size, position, badge category).

*   **Designed Badges** — SVG vector equivalents of Smart Badges. Selected the same way (by metadata match from the Badge Library), but use vector SVG files which scale perfectly at any size. Use Designed Badges when you have SVG assets and want crisp rendering at all poster dimensions. Configured via the same properties panel as Smart Badges.

*   **Title Logo** — A special badge that renders the movie or TV show title onto the poster. When **Enable Textless / Clean Posters** is on and a textless (language-neutral) poster is found, the title is rendered using a clearlogo PNG from TMDB — a transparent logo image in the style of the title's official branding. If no clearlogo exists, falls back to rendering the title text using the configured font. The Title Logo is the main reason to use textless posters: it lets you place the title at any position with any styling, instead of relying on the baked-in title from the standard poster. Configured via the **Title Logo Properties** panel when selected (position mode, anchor point, size, font fallback settings).

**Layout Badges List:**

Below the palette is a list of all badges currently in the layout. Clicking an item in this list selects it on the canvas and opens its properties in the right panel. Use the **delete button** (×) next to each item to remove it from the layout.

---

**Center Panel — Canvas:**

The main working area. The canvas represents your 600×900 poster space.

**Toolbar Controls:**

*   **Layout Name:** The name of the layout as it will appear in dropdowns on the Overlay Management page. Give it a descriptive name (e.g., "Movie — Bottom Tray with Audio" or "Show — Top Corner Ratings").

*   **Media Type:** Sets which media type this layout applies to:
    *   **Movies** — Appears only in the Movies layout selector.
    *   **TV Shows** — Appears only in the TV Shows layout selector.
    *   **Season Posters** — Appears only in the Season layout selector.
    *   **Both (Movies & TV Shows)** — Appears in both Movies and TV Shows selectors.

*   **Load Poster:** Upload a local image file to use as the canvas background for preview purposes. This does not affect the layout itself — it's just for visualizing how your badges will look on a real poster.

*   **← / Next Poster:** Cycles through real posters from your library loaded into a preview pool. Use these to see how the layout looks across different artwork. The counter shows your current position in the pool.

*   **Reset:** Clears the poster preview and returns to the default gradient background. Your badge layout is not affected.

*   **Save:** Saves the current layout to the database. If you are editing an existing layout, this overwrites it. The layout name must be filled in before saving.

*   **Clear:** Removes all badges from the canvas and resets the layout to empty. This does not delete the saved layout — it only clears the current editor state.

*   **Zoom (− / + / click to fit):** Adjusts the canvas zoom level. Click the zoom percentage label to auto-fit the canvas to the available space.

*   **Grid:** Toggles an alignment grid overlay on the canvas to help with precise positioning.

*   **? (Variable Legend):** Opens a reference panel listing every available template variable with its description. Useful when writing custom text badge expressions.

**Working on the Canvas:**

*   Click a badge on the canvas to select it (highlighted in blue). Its properties appear in the right panel.
*   Drag a badge to reposition it. The X/Y coordinates in the right panel update in real time.
*   Drag from the palette directly onto the canvas to add a badge at a specific position.
*   Click an empty area of the canvas to deselect.

---

**Right Panel — Layout Info:**

Always visible at the top of the right panel.

*   **Description:** An optional free-text description of the layout. Shown on the layout card on the Overlay Management page to help you remember what the layout is for.

---

**Right Panel — Preview Data:**

A collapsible section that controls the sample metadata values used when previewing the layout on the canvas. Changing these values lets you see how badges will look for different types of content without leaving the builder.

*   **Resolution:** 2160p (4K), 1080p, 720p, 480p
*   **HDR:** Dolby Vision, DV + HDR, DV + HDR10, DV + HDR10+, HDR10+, HDR10, HDR, or None
*   **Audio Codec:** Full list including TrueHD Atmos, DTS-X, DTS-HD MA, Dolby Digital+, AAC, FLAC, and more
*   **Channels:** 7.1, 5.1, 2.0, Mono
*   **Video Codec:** HEVC, AVC, AV1, VP9
*   **Format:** Blu-ray, WEB-DL, WEBRip, HDTV, DVD
*   **Content Rating:** R, PG-13, PG, G, TV-MA, TV-14, TV-PG
*   **Network / Studio:** Free text — type any network or studio name
*   **Show Status:** Returning, Airing, Ended, Canceled

These values feed the `{{variable}}` substitution in text badges and also control which PNG/SVG image is selected for smart/designed badges.

---

**Right Panel — Tray (Background Panel) Properties:**

Visible when a Tray element is selected.

*   **Position & Size:**
    *   **X / Y:** Position of the tray's top-left corner in canvas pixels (0–600, 0–900).
    *   **Width / Height:** Dimensions of the tray in canvas pixels. A typical bottom-strip tray might be 582×58, centered at x=9.

*   **Overall Opacity:** Controls the transparency of the entire tray including all its elements (0–100%).

*   **Border:** (toggle-able)
    *   **Color:** Border color picker.
    *   **Width:** Border stroke width in pixels.
    *   **Opacity:** How transparent the border is.
    *   **Border Radius:** Rounds the tray corners (0 = sharp, higher = more rounded).

*   **Base Background:**
    *   **Type:** Solid (single color) or Gradient (two-color linear gradient).
    *   **Color 1 / Color 2:** Background colors. Color 2 only visible in Gradient mode.
    *   **Angle (deg):** Gradient direction — 0° is left-to-right, 90° is top-to-bottom, 135° is diagonal.
    *   **Opacity:** How transparent the background fill is.
    *   **Padding:** Internal padding between the tray edge and its content.

---

**Right Panel — Badge Properties:**

Visible when any badge (text, smart, or designed) is selected.

*   **Position:**
    *   **X / Y:** Badge position in canvas pixels. Top-left anchor by default. Drag the badge on canvas for convenience, or type exact values here for precision.

*   **Badge Type:** A dropdown to change what data/image this badge displays:
    *   **Rating:** IMDb Rating, TMDb Rating, RT Critics, RT Audience, Trakt Rating
    *   **Media Info:** Resolution, HDR Format, Audio Codec, Audio Channels, Video Codec, Format/Source
    *   **Library:** Network, Studio, Content Rating, Show Status, Versions/Duplicates
    *   **Custom:** A freeform text/template expression of your own design
    *   **File Match:** A conditional badge that only renders when the video filename contains a specified search term — see below for details
    *   Changing the type applies that type's default style preset. Your position is preserved but visual properties reset to defaults for the new type.

*   **Background:** (toggle-able)
    *   **Opacity:** Background fill transparency.
    *   **Color:** Background fill color (color picker + hex input for full RGBA).
    *   **Width / Height:** Fixed dimensions (0 = auto-size to fit content).
    *   **Border Radius:** Corner rounding.
    *   **Padding:** Space between the badge content (icon + text) and the background edge.
    *   **Border Width / Border Color:** Optional stroke around the background box.

*   **Icon / Logo:** (toggle-able)
    *   **Type:** Image (PNG/JPG) or SVG Vector.
    *   **Path:** The path to the icon file. Click the **Browse** button (gallery icon) to open the Logo Picker modal which shows all available logos organized by category.
    *   **Width / Height:** Icon dimensions in pixels (0 = auto).
    *   **Side:** Where the icon appears relative to the text label:
        *   **Left of text** — Icon appears to the left of the text.
        *   **Right of text** — Icon appears to the right.
        *   **None (use X/Y offset)** — Icon is placed independently using X/Y offset controls. Use this when you want the icon centered in the badge with no text, or when using X/Y offset to fine-tune placement.

*   **Text / Value:** (toggle-able)
    *   **Vertical Stack:** When enabled, splits the badge into two vertically stacked text lines. A gap value controls the spacing between the lines. Useful for badges like "DOLBY / ATMOS" displayed on two lines.
    *   **Variable:** The template variable that drives the text content. Select from the dropdown:
        *   Ratings: `{{imdbRating}}`, `{{tmdbRating}}`, `{{traktRating}}`, `{{rtCriticsScore}}`, `{{rtUserScore}}`
        *   Video: `{{resolution}}`, `{{hdr}}`, `{{videoCodec}}`
        *   Audio: `{{audioCodec}}`, `{{audioChannels}}`
        *   Format: `{{format}}`
        *   Library: `{{network}}`, `{{studio}}`, `{{contentRating}}`, `{{status}}`, `{{year}}`, `{{versionCount}}`
        *   Select **"— Custom expression —"** to write a mixed text + variable expression, e.g. `Score: {{imdbRating}}` or a static label.
    *   **Font:** Font family for the text. Available fonts are loaded from the system (includes Google Fonts and any user-uploaded fonts).
    *   **Font Weight:** Normal or Bold.
    *   **Font Style:** Normal or Italic.
    *   Additional text styling options (size, color, alignment) may appear depending on the badge type.

---

**Logo Picker Modal:**

Opens from the Icon/Logo path browse button. Displays all available logo images grouped by category (audio, video, ratings, networks, etc.).

*   **Tabs:** Filter logos by category.
*   **Search:** Filter the grid by filename.
*   **Upload:** Upload a new logo PNG or SVG from your device. Uploaded logos are stored in your user assets directory and available across all layouts.
*   Clicking a logo selects it and closes the modal, inserting the path into the Icon/Logo path field.

---

**Template Variable Legend Modal:**

Opens from the **?** button in the canvas toolbar. Lists every available `{{variable}}` with a description of what value it produces at render time. Use this as a reference when writing custom text badge expressions.

---

**Workflow: Creating Your First Layout:**

1.  Click **Create Layout** on the Overlay Management page, or navigate directly to `/overlays/builder`.
2.  Enter a **Layout Name** and select a **Media Type**.
3.  Click **Next Poster** a few times to load real library posters into the preview.
4.  Add a **Tray** from the palette and drag it to the bottom of the canvas. Set its background color and opacity.
5.  Add a **Text Badge** from the palette. Set its type to "Audio Codec" in the right panel. Position it inside the tray.
6.  Add more badges as desired (resolution, HDR, ratings, etc.).
7.  Use the **Preview Data** section to test different metadata combinations and confirm all badges look correct.
8.  Click **Save**.
9.  Return to the Overlay Management page, select the new layout in the Movies or TV Shows tab, and click **Generate Selected** on a few items to test before running **Generate Library**.

---

**File Match Badge:**

The **File Match** badge type renders only when the video filename contains a specified search term. This is useful for flagging attributes that aren't available as standard metadata — such as extended cuts, director's cuts, specific encode groups, or any keyword present in the filename.

*   **File Match section** (visible only when badge type is set to File Match):
    *   **Search Term:** The text to look for in the filename. The match is case-insensitive — `REMUX`, `remux`, and `Remux` all match the same files.
    *   **Display Text:** The text shown on the badge when a match is found. Leave empty to display the search term itself.
    *   **Use icon instead of text:** When enabled, the badge shows the icon configured in the Icon/Logo section instead of any text. Use this to show a logo rather than a label.

*   **Styling:** All other badge properties work exactly as with any other badge type — configure the Background, Icon, and Text/Value sections to control how the badge looks when it appears.

*   **Preview:** The canvas always shows the badge (using the display text or search term as a placeholder) so you can design its appearance regardless of whether a match exists. At render time, if the filename does not contain the search term, the badge is completely hidden.

*   **Example uses:**
    *   Search term `EXTENDED` — badge appears only on extended cut files
    *   Search term `REMUX`, display text `REMUX` — styled badge visible only on remux files
    *   Search term `x265` — badge visible only on x265 encodes, useful when the video codec badge doesn't distinguish x264 vs x265 source clearly

---

**Tips:**

*   Use the **Grid** toggle to align badges precisely — especially useful when lining up multiple badges at the same Y position.
*   The canvas is 600×900 pixels but the poster reference space. Badges placed at x=0 or y=0 are at the very edge of the poster — leave a few pixels of margin.
*   Smart Badges (Library Badges) require PNG assets to be uploaded in the Badge Library. If no asset matches the item's metadata, that badge slot is silently skipped during rendering.
*   When a badge's text variable is empty for a given item (e.g., `{{network}}` for a movie), the badge is hidden automatically — your layout handles movies and shows gracefully with a single layout when set to "Both".
*   Use **Vertical Stack** in text badges to display multi-line codec labels (e.g., showing "DOLBY" and "ATMOS" stacked).
*   Use **Duplicate** on an existing layout to create a variant (e.g., a version with different colors for season posters) without starting from scratch.
*   The **Load Poster** feature is for your own preview only. Load a dark poster to design a light-text layout, and a light poster to verify visibility of dark elements.
