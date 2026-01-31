### Plex Labels Help

This guide explains how Plex labels work in the application and how to manage them using the functions available in Debug Functions.

**What Are Plex Labels?**

Plex labels are tags applied to movies and TV shows in your Plex library that help you organize and identify content by its source or requester. The application automatically manages these labels based on your content source configuration.

*   **Track Requesters:** See who requested each movie or show (e.g., "Requested by John")
*   **Identify Sources:** Know which content source added an item (e.g., "Overseerr", "Trakt List: Family Movies")
*   **Create Smart Collections:** Use Plex's smart filter feature to create collections based on labels
*   **Monitor Usage:** Track which users are actively requesting content

**How Labels Are Applied:**

Labels are automatically applied in two ways:

*   **During Collection:** When an item is successfully collected, labels are generated and synced immediately
*   **Manual Sync:** You can manually sync labels for existing items using the Debug Functions

**Label Configuration:**

Configure labels per content source in Settings → Content Sources:

*   **Label Mode:** `Fixed` (static label), `List Name` (content source display name), or `Requester` (actual requester's name)
*   **Enabled/Disabled:** Toggle whether labels should be applied for this source

**Plex Label Management Utilities:**

**View Items by Label**

*   **Purpose:** Search for items that have a specific Plex label applied
*   **How to Use:** Enter a label name and click Search to see all items with that label
*   **Use When:** Verifying which items have specific labels, auditing label application

**Bulk Apply Labels from Source**

*   **Purpose:** Apply Plex labels to all existing items from a specific content source retroactively
*   **What It Does:** Applies labels to items that were added before Plex labels were configured for that source
*   **How to Use:** Select a content source from dropdown, click Preview to see what will be labeled, then Apply Labels
*   **Use When:** After enabling Plex labels for an existing content source that already has collected items

**Bulk Remove Labels**

*   **Purpose:** Remove a specific label from all items in the database and Plex
*   **What It Does:** Searches for items with the specified label and removes it from both database and Plex
*   **How to Use:** Enter the label name to remove, click Preview to see affected items, then Remove Label
*   **Use When:** Cleaning up incorrect labels, removing labels after disabling a content source

**Orphaned Label Cleanup**

*   **Purpose:** Find and remove labels tracked in the database but no longer exist in any content source configuration
*   **What It Does:** Identifies labels that were created but the source is now disabled or removed, then removes them from database and Plex
*   **How to Use:** Click Find Orphaned Labels to scan, then Cleanup to remove them
*   **Use When:** After removing or disabling content sources, general database cleanup

**Sync Labels from Content Sources** (Sync All Labels button)

*   **Purpose:** Re-sync all labels based on current content source configurations
*   **What It Does:** Applies labels to all Collected items from sources that have Plex labels enabled
*   **Use When:** After making changes to label configurations, general label sync
*   **Note:** This is similar to the "Full Sync" function but accessed via button instead of dropdown

**Available Functions (Debug Functions → Run Task Manually):**

**1. Backfill Plex Labels Content Source Detail**

*   **Purpose:** Fills in missing or unknown requester names in the database
*   **What It Does:** Queries Overseerr API to retrieve actual requester names for items showing "Unknown", updates the `content_source_detail` field, does **not** sync labels to Plex (only updates database)
*   **Use When:** Upgrading from an older version that didn't track requester names, you see "Unknown" as the requester for Overseerr items, or after restoring from a backup with incomplete data
*   **Time:** 5-15 minutes depending on API responses

**2. Sync Labels from Content Sources (Full - All Items)**

*   **Purpose:** Processes ALL collected items and syncs labels to Plex
*   **What It Does:** Processes all collected items (typically 5000+ items), regenerates Plex labels from current content_source_detail values, syncs all labels to Plex, and sets timestamp tracking for future incremental syncs
*   **Use When:** First time setup after enabling Plex labels, after running "Backfill Content Source Detail", after bulk changes to content source label configuration, troubleshooting label issues, or after rebuilding your Plex library
*   **Time:** 13-14 hours for 5922 items
*   **Note:** Shows progress updates every 20 items in the Logs page

**3. Sync Labels from Content Sources (Incremental - Last 7 Days)**

*   **Purpose:** Processes only changed or new items (much faster)
*   **What It Does:** Only syncs items with `plex_labels_last_synced = NULL` (never synced), OR items collected in the last 7 days, generates and syncs labels to Plex, and sets timestamp
*   **Use When:** **Recommended for daily/weekly maintenance**, after adding new content to your library, or regular scheduled syncs to catch changes
*   **Time:** 5-15 minutes (processes 5-30 items typically)
*   **Performance:** 95%+ time reduction compared to full sync

**4. Backfill Missing Labels**

*   **Purpose:** Generates labels for items with NULL or empty labels field
*   **What It Does:** Finds items with NULL/empty `plex_labels` field in database, generates labels from `content_source_detail`, syncs newly generated labels to Plex, and **preserves existing labels** on other items (non-destructive)
*   **Use When:** Some items are missing labels in Plex but should have them, after enabling Plex labels for a source that already had collected items, or error recovery for items that failed label generation during collection
*   **Time:** Varies depending on how many items have NULL labels
*   **Note:** Safe to run multiple times - won't affect items that already have labels

**Comparison Table:**

| Function | Processes | Updates Database? | Syncs to Plex? | Time | Use Case |
|----------|-----------|-------------------|----------------|------|----------|
| Backfill Content Source Detail | Items with NULL/Unknown requester | ✅ Requester names only | ❌ | Minutes | Fix missing names |
| Full Sync | ALL items (~5000+) | ✅ Labels | ✅ | 13-14 hours | Initial setup, troubleshooting |
| Incremental Sync | Changed/new items (~5-30) | ✅ Labels | ✅ | 5-15 minutes | Routine maintenance |
| Backfill Missing Labels | Items with NULL labels | ✅ Labels | ✅ | Varies | Catch-up, error recovery |

**Recommended Workflow:**

**Initial Setup (New Installation or Enabling Labels):**

*   Configure Label Settings (Settings → Content Sources)
*   Run: "Backfill Plex Labels Content Source Detail" (5-15 minutes)
*   Run: "Sync Labels from Content Sources (Full - All Items)" (13-14 hours - run overnight)
*   Verify in Plex that labels appear correctly

**Routine Maintenance:**

*   **Daily or Weekly:** Run "Sync Labels from Content Sources (Incremental - Last 7 Days)" (5-15 minutes)
*   **As Needed:** Run "Backfill Missing Labels" if you notice any items missing labels

**After Configuration Changes:**

*   Optional: Run "Backfill Plex Labels Content Source Detail" (if requester data might have changed)
*   Run: "Sync Labels from Content Sources (Full - All Items)" to ensure all items reflect the new configuration

**Troubleshooting:**

**Labels Not Appearing in Plex:**

*   Check Settings → Content Sources → verify labels are enabled
*   Run "Backfill Plex Labels Content Source Detail"
*   Run "Sync Labels from Content Sources (Full - All Items)"
*   Force metadata refresh in Plex

**Labels Show "Unknown" Instead of Names:**

*   Run: "Backfill Plex Labels Content Source Detail" (fills in names from Overseerr)
*   Run: "Sync Labels from Content Sources (Full - All Items)" (applies new names to Plex)

**Full Sync Taking Too Long:**

*   This is normal - processing 5000+ items at 2-3 items/second takes 13-14 hours
*   Let it run overnight
*   Monitor progress in Logs page (updates every 20 items)
*   Use incremental sync for routine maintenance (5-15 minutes)
*   If interrupted, safe to restart (won't duplicate labels)

**Some Items Have Labels, Others Don't:**

*   Run: "Backfill Missing Labels" (catches items with NULL labels)
*   Check logs for error messages about specific titles
*   Verify items exist in Plex (Database page)

**Technical Details:**

**Database Fields:**

*   `content_source`: Which list/service added the item
*   `content_source_detail`: The specific requester name or list name
*   `plex_labels`: JSON field storing label(s) to apply
*   `plex_labels_last_synced`: Timestamp of last successful sync (used for incremental mode)

**TV Show Handling:**

*   Labels are applied at the **show level**, not per episode
*   When processing 100 episodes of a show, only **1 API call** is made to Plex
*   All episodes of a show share the same labels
*   This optimization significantly reduces processing time

**Rate Limiting:**

*   Processes approximately 2-3 items per second
*   Automatically throttles requests to avoid Plex API rate limits
*   This is why full sync takes significant time

**Best Practices:**

*   **Initial Setup:** Always run full sync first to establish baseline
*   **Routine Maintenance:** Use incremental sync (much faster)
*   **Monitor Progress:** Check logs when running full sync
*   **Verify Results:** Spot-check items in Plex after sync completes
*   **Schedule Wisely:** Run full sync during off-hours due to duration
*   **Configuration Changes:** Re-run full sync after changing label settings
*   **Error Recovery:** Use "Backfill Missing Labels" to catch stragglers

**Advanced Usage:**

**Custom Sync Intervals:**

You can change the 7-day window using the API endpoint:

```bash
# Sync items from last 30 days
curl -X POST http://localhost:5000/debug/sync_plex_labels \
  -H "Content-Type: application/json" \
  -d '{"incremental": true, "days_back": 30}'
```

**Related Settings:**

*   Settings → Content Sources → [Source Name] → Plex Labels
*   Settings → Plex → Plex URL and authentication (required for label sync)
