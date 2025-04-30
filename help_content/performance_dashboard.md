### Performance Dashboard Help

This page provides a real-time overview of the application's resource usage and performance metrics.

**Header:**

*   **Export Data:** Click the <i class="fas fa-download"></i> button to download the raw performance data currently loaded on the page as a JSON file.

**System Resources:**

This card shows the current instantaneous resource usage.

*   **CPU Usage:**
    *   **Progress Bar:** Visual representation of the current overall CPU percentage used by the application process.
    *   **Details:** Shows the total `User Time` (time spent executing application code) and `System Time` (time spent executing kernel code on behalf of the application) in seconds.
*   **Memory Usage:**
    *   **Progress Bar:** Visual representation of the memory usage percentage. This is calculated based on the *Resident Set Size (RSS)* compared to the *Virtual Memory Size (VMS)*.
    *   **Details:**
        *   `RSS Memory:` Resident Set Size - The portion of the process's memory held in physical RAM. This is often the most relevant measure of actual memory usage.
        *   `Virtual Memory:` Virtual Memory Size (VMS) - The total amount of virtual address space used by the process.
        *   `Swap Used:` The amount of swap space currently used by the process (if applicable).

**Memory Analysis:**

This card provides a deeper look into memory usage patterns over the last hour.

*   **Summary Metrics:**
    *   `Average RSS Memory:` Average RSS usage over the period.
    *   `Peak RSS Memory:` Highest RSS usage recorded during the period.
    *   `Average Virtual Memory:` Average VMS usage over the period.
    *   `Peak Virtual Memory:` Highest VMS usage recorded during the period.
*   **Memory Types:**
    *   `Anonymous Memory:` Memory not backed by a file (e.g., program stack, heap). Shows total size and number of mappings.
    *   `File-backed Memory:` Memory mapped directly from files on disk (e.g., shared libraries, memory-mapped files). Shows total size and number of mappings.
*   **Open Files:**
    *   Lists files currently held open by the application process, showing the filename (hover for full path) and size.

**Memory Growth:**

This section tracks memory usage over time (typically the last hour).

*   **Chart:** A line chart showing the history of `RSS Memory` (Blue) and `Virtual Memory` (Purple) usage over the period. Hover over points for exact values and timestamps.
*   **Recent Values:** Below the chart, a list shows memory snapshots (RSS, VMS, Swap) at recent intervals (e.g., every 10 minutes), providing a quick look at recent trends.

**Resource Handles:**

Provides a summary of system resources being used.

*   `Open Files:` Total count of files currently open by the process.
*   `File Types:` A comma-separated list of unique file extensions found among the open files.

**CPU Profile:**

Focuses on CPU utilization patterns over the last hour.

*   **Summary Statistics:**
    *   `Average CPU:` Average CPU percentage used over the period.
    *   `Peak CPU:` Highest CPU percentage recorded during the period.
    *   `CPU Time:` Total `User` and `System` CPU time consumed during the period.
*   **Chart:** A line chart showing the history of CPU usage percentage over the period. Hover over points for exact values and timestamps.
*   **Active Threads:**
    *   Shows the current number of active threads within the application process.
    *   Lists individual threads, showing their `User Time`, `System Time`, and a small bar visualizing the proportion of user vs. system time for that thread.

**Data Refresh:**

*   The data on this dashboard automatically refreshes approximately every 60 seconds.
