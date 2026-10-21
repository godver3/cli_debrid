### Badge Library Help

The Badge Library is where you manage the PNG and SVG image assets used by **Smart Badges** and **Designed Badges** in your overlay layouts. When the overlay system renders a poster, it looks up the correct badge image from this library based on the media item's actual metadata — so the right audio codec logo, resolution badge, or video codec icon is selected and applied automatically.

**What Are Badge Assets?**

Badge assets are image files (PNG or SVG) that represent specific media attribute values — for example, a styled "DTS-HD Master Audio" logo, a "4K UHD" resolution badge, or a "Dolby Vision" HDR type icon. You supply these images; the system matches and places them. The quality, style, and design of the badge images is entirely up to you — use the bundled sets, download third-party packs, or design your own.

---

**Page Layout:**

*   **Back to Overlays:** Returns to the Overlay Management page.

*   **Stats Bar:** Summary counts at the top of the page:
    *   **Badge Types:** How many badge categories are registered in the system (e.g., Audio Codec, Video Codec, HDR Format, etc.).
    *   **Total Slots:** The total number of individual variation slots across all badge types. Each variation slot corresponds to one specific metadata value (e.g., "TrueHD Atmos" is one slot under Audio Codec).
    *   **Assets Uploaded:** How many slots have an image file uploaded.
    *   **Missing Assets:** How many slots are still empty. Badges with missing assets are silently skipped during overlay rendering — no error, the badge just does not appear.

*   **Category Tabs:** Filter the badge type cards by category:
    *   **All** — Shows every badge type.
    *   **Audio** — Audio codec badges (TrueHD Atmos, DTS-X, Dolby Digital+, etc.).
    *   **Video** — Video codec and resolution/HDR badges.

---

**Badge Type Cards:**

Each badge category is represented as a card. The card header shows:

*   **Display Name:** The human-readable name for this badge type (e.g., "Audio Codec", "HDR Format").
*   **Composite** label (purple): If shown, this badge type combines multiple metadata fields. For example, an audio badge that shows both codec and channel count in a single image is a composite type.
*   **Category:** Which category this type belongs to (audio or video).
*   **Fields:** The metadata fields this type reads from to determine which image to show. For example, an audio badge might read both `audio_codec` and `audio_channels`.
*   **X/Y assets uploaded:** A count of how many variation slots in this type have images.

**Variation Grid:**

Below the header, each slot in the badge type is shown in a grid. Each slot represents one specific combination of metadata values that could be matched.

*   **Filled slot:** Shows the uploaded badge image as a preview thumbnail.
*   **Empty slot:** Shows a dashed placeholder indicating no image has been uploaded yet.
*   Each slot displays a label (the variation name, e.g., "TrueHD Atmos 7.1") and a small **source tag** in the top-left corner indicating whether the asset is from the default system set or a user upload.

**Uploading an Asset:**

*   Click anywhere on a variation slot (or on the image thumbnail if one exists) to open a file picker.
*   Select a PNG, JPG, or SVG file from your device.
*   The image uploads immediately and the slot refreshes to show the new preview.
*   Uploaded images replace any previously uploaded image for that slot.
*   There is no enforced size requirement, but badge images are typically small — 180×60 pixels is a common size for audio/video badges. The overlay system scales them to the size configured in the Layout Builder.

**Removing an Asset:**

*   Hover over a filled slot to reveal action buttons.
*   Click the delete/remove button to clear the image from that slot. The slot reverts to an empty placeholder.

---

**Adding Custom Variations:**

Each badge type card has an **+ Add Custom Variation** button at the bottom.

*   Use this when you need a slot for a metadata value combination that is not already present in the default variation list.
*   **Variation Key:** The internal identifier used when matching metadata. For example, `truehd_atmos|7.1` for a TrueHD Atmos 7.1 badge, or `dts-x` for a DTS:X badge. The key format depends on the badge type — refer to existing variation keys in the same type card as a guide.
*   **Display Name:** A human-readable label shown in the badge library (e.g., "TrueHD Atmos 7.1"). This does not affect matching — only the key does.
*   Click **Add Slot** to create the variation. You can then upload an image to it.

Custom variations you add are preserved across updates. If you have badge image packs with channel-specific variants (e.g., separate DTS-HD 5.1 and DTS-HD 7.1 images), add the corresponding variation slots here and upload each image to its matching slot.

---

**How Automatic Badge Selection Works:**

When the overlay system renders a poster for a media item, for each Smart Badge or Designed Badge in the layout it:

1.  Reads the relevant metadata fields from the item (e.g., `audio_codec = "TrueHD Atmos"`, `audio_channels = "7.1"`).
2.  Constructs a lookup key from those fields (e.g., `truehd_atmos|7.1`).
3.  Searches the Badge Library for a variation slot with that key under the matching badge type.
4.  If found and an image is uploaded, uses that image.
5.  If not found at the full specificity, may fall back to a less specific match (e.g., `truehd_atmos` without a channel variant).
6.  If no match at all, the badge slot is skipped — no image is drawn in that position on the poster.

This means:
*   Uploading a `truehd_atmos` slot (no channel) creates a fallback used for all TrueHD Atmos tracks regardless of channel count.
*   Uploading both `truehd_atmos` (general) and `truehd_atmos|7.1` (specific) means 7.1 items use the specific image while all other channel counts fall back to the general one.

---

**Supported Badge Types:**

The badge library typically includes these categories:

*   **Audio Codec** — Matches on audio codec (and optionally channel count). Covers TrueHD Atmos, TrueHD, DTS-X, DTS-HD Master Audio, DTS-HD HRA, DTS-HD, DTS-ES, DTS, Dolby Atmos, Dolby Digital+ Atmos, Dolby Digital+, Dolby Digital, AAC, FLAC, PCM, MP3, Opus, and more.
*   **Video Codec** — Matches on video codec. Covers HEVC/H.265, AVC/H.264, AV1, VP9, and others.
*   **Resolution** — Matches on resolution. Covers 4K/2160p, 1080p, 720p, 480p.
*   **HDR Format** — Matches on HDR type. Covers Dolby Vision, HDR10+, HDR10, HLG, HDR (generic), and combinations (DV+HDR10, etc.).
*   **Format/Source** — Matches on release source. Covers Blu-ray, WEB-DL, WEBRip, HDTV, DVD.
*   **Ratings** (if configured) — May include IMDb, TMDb, Rotten Tomatoes logo assets for use alongside rating text badges.

Composite types combine multiple fields — for example, an "Audio with Channels" composite reads both codec and channel count to select an image like "DTS-HD Master Audio 5.1" as a single combined badge.

---

**Organizing Your Badge Assets:**

*   The badge library accepts any PNG, JPG, or SVG image. There is no fixed canvas size enforced on upload, but consistency in dimensions across a set produces the most uniform appearance on posters.
*   Common badge dimensions: 180×60 px for horizontal audio/video badges. Square badges (e.g., 60×60) for small icon-style badges.
*   Badge images are rendered at the size specified in the Layout Builder for that badge element. Uploading high-resolution source images and letting the renderer scale them down is fine.
*   SVG assets (Designed Badges) scale perfectly regardless of the size set in the layout.

---

**Tips:**

*   Fill in the most commonly used audio and video codec slots first — most library items will match TrueHD Atmos, DTS-X, Dolby Digital+, HEVC, 4K, HDR10, and Dolby Vision.
*   The **Missing Assets** count is your to-do list — work through it until it reaches zero for a complete badge set.
*   Use the **Category tabs** (Audio / Video) to focus on one badge category at a time when doing a bulk upload session.
*   If a badge is never appearing on posters, check two things: (1) Is the variation slot for that metadata value present and has an image uploaded? (2) Is the Layout Builder badge element configured as a Smart Badge or Designed Badge (not a Text Badge)?
*   Custom badge image packs (downloaded from the community or created in a design tool) can be bulk-added by adding variation slots and uploading each file individually. Slot keys must match the metadata values the system expects — copy existing keys as a reference.
*   After uploading new badge assets, re-run **Generate Library** (or **Regenerate All**) from the Overlay Management page to apply the new images to your existing poster overlays.
