### PhalanxDB Status Help

This page provides diagnostic information about the PhalanxDB service, which is used for distributed hash checking.

**Connection Status:**

*   This section shows the current connection state to the underlying PhalanxDB service.
    *   **Disabled:** If PhalanxDB is turned off in your application settings, a warning message will be displayed here, and the rest of the page details will be hidden.
    *   **Connected:** Indicates a successful connection to the PhalanxDB service.
    *   **Not Connected:** Indicates that the application failed to connect to the PhalanxDB service. Check your PhalanxDB configuration and ensure the service is running correctly. This message could appear if the service is running but failing to respond due to heavy load.

**(The following sections are only visible if PhalanxDB is enabled and connected)**

**Node Status:**

This card provides information about your local PhalanxDB node instance.

*   **Node Information:**
    *   `Node ID:` The unique identifier for your PhalanxDB node in the network.
    *   `Database Entries:` The total number of hash entries currently stored by your node.
    *   `Last Sync:` The timestamp when your node last exchanged data with other peers in the network.
*   **Memory Usage:** Shows metrics about the memory consumption of the PhalanxDB process:
    *   `RSS:` Resident Set Size (total physical memory allocated).
    *   `Heap Total:` Total size of the JavaScript heap.
    *   `Heap Used:` Amount of the JavaScript heap currently in use.
    *   `External:` Memory used by C++ objects bound to JavaScript objects.

**Network Stats:**

This card displays statistics related to PhalanxDB's network activity.

*   `Active Connections:` The number of peers your node is currently connected to.
*   `Syncs Sent:` The number of synchronization messages your node has sent to peers.
*   `Syncs Received:` The number of synchronization messages your node has received from peers.

**Hash Tester:**

This tool allows you to manually check the status of a specific hash within the PhalanxDB network.

*   **Usage:**
    1.  Enter a torrent info hash into the input field.
    2.  Click the `Test Hash` button.
*   **Results:**
    *   `Status:` Indicates whether the hash was found and its cache status (`Cached` or `Not Cached`). If the hash isn't in the database at all, it will show `Not Found`. Errors during the check will be displayed here.
    *   `Last Modified:` The timestamp when the status for this hash was last updated in the database (only shown if found).
    *   `Expires:` The timestamp when the entry for this hash is expected to expire from the database (only shown if found).
