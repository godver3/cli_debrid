### NFO Files Help

NFO files are small text files next to your episode files that carry Clyde's title, plot and air date for exactly the episode in that file. Plex, Jellyfin, Emby and Kodi can read them. You find the page under **Tools → NFO Files**.

**Why it matters:** From 3 December 2026 Plex numbers TV shows by TMDB. Clyde names episode files by its own metadata (TVDB numbering, or the episode order you picked for a show). Where the two disagree, Plex shows the wrong episode titles. NFO files fix that.

**Requirements:**

*   **Symlinked/Local** file management. In Plex (Direct Mount) mode Clyde has no library folder to write to, so the page shows a warning and only **Off** can be picked.
*   The Plex cards only appear when your media server is Plex. The snapshot card also needs Symlinked/Local.
*   Settings can only be changed by an admin.

**NFO files for my TV library:**

Pick one mode. Clyde asks you to confirm.

*   **Off:** No NFO files. Plex shows its own details. Shows whose numbering differs from Plex's (custom orders, many anime) show Plex's episode details from 3 December. Turning Off also puts Plex's previous agent back and deletes the NFO files Clyde wrote. Snapshot NFO files come back in their place.
*   **Shows with a changed episode order:** NFO files only for shows you set to another order (Show options or Manual Assign). They are written automatically when you change the order. Other shows look as Plex makes them.
*   **All shows:** Every show gets NFO files. Where Plex already numbers an episode the same way, the file is minimal, so Plex still supplies the title, description, thumbnail and cast. Only episodes Plex would mix up get Clyde's details. This also fixes default-order shows that TMDB numbers differently.

*   **Switch my Plex TV library to the NFO agent when NFO files are turned on:** Ticked, turning NFO files on also switches the Plex TV library to the NFO agent and refreshes it. Unticked, Clyde only writes the NFO files and Plex stays exactly as it is. You can switch later with **Set up Plex agent**.
*   NFO files go under the symlinked files path shown on the page: a `tvshow.nfo` (ids only) and one `.nfo` per episode file. They are kept up to date when episodes are added, upgraded, removed or renumbered.
*   NFO files you made yourself are never changed or deleted.

**Library:**

*   **Convert entire library:** Rewrites every show's NFO files, then sets up Plex and refreshes the library. It is greyed out while NFO files are Off or a job is running.

**Plex:**

*   **Set up Plex agent:** Switches the TV libraries that read the symlinked folder to the agent Clyde makes, **Clyde NFO + Plex Series**, and refreshes them. It reads the NFO file first, then Plex Series for anything the NFO doesn't say (cast, artwork, Discover), then local media. Shows keep their Plex identity, so watch history and collections stay. Needs Plex Media Server 1.43.1 or newer.
*   **Restore Plex's previous agent:** Puts the libraries back on the agent they had before.
*   **Check:** Reloads the table of TV libraries, their folders and the agent each one uses.

**Check a show in Plex:**

*   Type a title and click **Find**. The list shows each show's episode order, how many files it has, and whether it gets NFO files.
*   Click a show to compare every episode file with what Plex shows for that season and episode. The result lists how many are right, wrong or not in Plex yet, with the title Plex shows and the one it should show.
*   **NFO files for this show** is a per-show override: **Follow the mode**, **Always full details** or **No NFO files**.
*   **Fix: rewrite and refresh in Plex** rewrites the show's NFO files and refreshes it in Plex. Check again after a minute.

**Save Plex's current episode details (snapshot):**

*   **Snapshot Plex now** reads what Plex shows today for every episode in the TV library (TheTVDB titles, descriptions, air dates and anything you edited by hand) and saves it as NFO files next to the episodes. Plex isn't changed.
*   After 3 December Plex can only refill episodes from TMDB, so take it before then. After that date a notice warns that a new snapshot saves TMDB's details for refreshed shows. Older snapshots are kept.
*   It works with NFO files Off. The files wait until you switch Plex (or Jellyfin, Emby, Kodi) to an agent that reads them.
*   NFO files Clyde makes for a mode, and NFO files you made yourself, are never replaced. Upgraded episodes keep theirs.
*   The list shows each snapshot with its date, episodes and shows. **Use this one** writes that snapshot's NFO files into the library and replaces the other snapshot files. **Rewrite** does it again for the one in use. **Delete snapshot NFO files** removes the files but keeps the snapshots.
*   Every snapshot is also kept in `plex_metadata_snapshot.db`.
