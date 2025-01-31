import json
import datetime
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from collections import defaultdict
import sys
import os

def parse_size_to_mb(size_str):
    """Convert size string to MB"""
    if isinstance(size_str, (int, float)):
        return float(size_str)
    try:
        if 'GB' in size_str:
            return float(size_str.replace('GB', '').strip()) * 1024
        elif 'MB' in size_str:
            return float(size_str.replace('MB', '').strip())
        elif 'KB' in size_str:
            return float(size_str.replace('KB', '').strip()) / 1024
        elif 'B' in size_str:
            return float(size_str.replace('B', '').strip()) / (1024 * 1024)
        return 0
    except:
        return 0

def parse_performance_log(log_path):
    """Parse the performance log JSON file"""
    metrics = []
    anon_mem = []
    net_states = []
    thread_data = []
    trace_data = []
    
    try:
        print(f"Reading log file: {log_path}")
        with open(log_path, 'r') as f:
            data = json.load(f)
            
        if not isinstance(data, dict) or 'entries' not in data:
            print("Invalid log format - missing 'entries' array")
            return [], [], [], [], []
            
        # Get the time span of the data
        entries = data['entries']
        if entries:
            start_time = datetime.datetime.fromisoformat(entries[0]['timestamp'])
            end_time = datetime.datetime.fromisoformat(entries[-1]['timestamp'])
            duration = end_time - start_time
            print(f"\nAnalyzing data spanning {duration}")
            print(f"Total data points: {len(entries)}")
            print(f"Average interval: {duration.total_seconds() / len(entries):.1f} seconds")
            
        print(f"\nLog file metadata:")
        print(f"- Start time: {data.get('metadata', {}).get('start_time', 'unknown')}")
        print(f"- System: {data.get('metadata', {}).get('system', 'unknown')}")
        print(f"- Python version: {data.get('metadata', {}).get('python_version', 'unknown')}")
        
        for entry in data['entries']:
            try:
                timestamp = datetime.datetime.fromisoformat(entry['timestamp'])
                entry_type = entry.get('type', '')
                
                if entry_type == 'basic_metrics':
                    metrics.append({
                        'timestamp': timestamp,
                        'cpu_percent': float(entry['metrics'].get('cpu_percent', 0)),
                        'rss_memory': float(entry['metrics'].get('memory_rss', 0)),
                        'vms_memory': float(entry['metrics'].get('memory_vms', 0)),
                        'system_memory_percent': float(entry['metrics'].get('system_memory_used', 0)),
                        'swap_used': float(entry['metrics'].get('swap_used', 0)),
                        'cpu_user_time': float(entry['metrics'].get('cpu_user_time', 0)),
                        'cpu_system_time': float(entry['metrics'].get('cpu_system_time', 0))
                    })
                elif entry_type == 'memory_growth':
                    trace_data.append({
                        'timestamp': timestamp,
                        'allocations': entry['metrics'].get('top_allocations', [])
                    })
                elif 'memory' in entry:
                    mem_data = entry['memory']
                    anon_mem.append({
                        'timestamp': timestamp,
                        'anonymous': mem_data.get('anonymous', {}),
                        'file_backed': mem_data.get('file_backed', {}),
                        'open_files': mem_data.get('open_files', {}),
                        'network': mem_data.get('network', {}),
                        'threads': mem_data.get('threads', {}),
                        'memory_maps': mem_data.get('memory_maps', [])
                    })
                elif entry_type == 'thread_stats':
                    thread_data.append({
                        'timestamp': timestamp,
                        'count': int(entry['metrics'].get('thread_count', 0)),
                        'stats': entry['metrics'].get('thread_details', [])
                    })
                elif entry_type == 'network_stats':
                    net_states.append({
                        'timestamp': timestamp,
                        'total': int(entry['metrics'].get('total_connections', 0)),
                        'states': entry['metrics'].get('connection_states', {})
                    })
            except Exception as e:
                print(f"Error parsing entry: {e}")
                continue
        
        print(f"\nSuccessfully parsed:")
        print(f"- {len(metrics)} system metric entries")
        print(f"- {len(anon_mem)} memory entries")
        print(f"- {len(net_states)} network state entries")
        print(f"- {len(thread_data)} thread data entries")
        print(f"- {len(trace_data)} memory growth entries")
        
        return metrics, anon_mem, net_states, thread_data, trace_data
    
    except Exception as e:
        print(f"Error reading log file: {str(e)}")
        return [], [], [], [], []

def analyze_results(system_metrics, anonymous_memory, network_states, thread_data, tracemalloc_data):
    print("\nAnalysis Results:\n")
    
    # Show system metrics trend
    if system_metrics:
        latest_metrics = system_metrics[-1]
        trend_window = system_metrics[-10:]  # Last 10 entries
        
        print("System Metrics Analysis:")
        print(f"- CPU Usage: {latest_metrics['cpu_percent']:.1f}% (trend: {'increasing' if trend_window[-1]['cpu_percent'] > trend_window[0]['cpu_percent'] else 'stable'})")
        print(f"- RSS Memory: {latest_metrics['rss_memory']:.1f} MB (trend: {'increasing' if trend_window[-1]['rss_memory'] > trend_window[0]['rss_memory'] else 'stable'})")
        print(f"- VMS Memory: {latest_metrics['vms_memory']:.1f} MB")
        print(f"- System Memory Usage: {latest_metrics['system_memory_percent']:.1f}%")
        print(f"- Swap Used: {latest_metrics['swap_used']:.1f} MB")
    
    # Show detailed memory breakdown
    if anonymous_memory:
        latest_memory = anonymous_memory[-1]
        anon_mem = latest_memory['anonymous']
        file_mem = latest_memory['file_backed']
        files = latest_memory['open_files']
        network = latest_memory['network']
        threads = latest_memory['threads']
        
        print("\nDetailed Memory Breakdown:")
        print(f"Anonymous Memory (not file-backed):")
        print(f"  - Total Size: {int(anon_mem.get('total_size', 0)) / (1024*1024):.1f} MB")
        print(f"  - Number of Mappings: {anon_mem.get('count', 0)}")
        
        print(f"\nFile-backed Memory:")
        print(f"  - Total Size: {int(file_mem.get('total_size', 0)) / (1024*1024):.1f} MB")
        print(f"  - Number of Mappings: {file_mem.get('count', 0)}")
        
        print(f"\nOpen Files:")
        print(f"  - Count: {files.get('count', 0)}")
        total_file_size = sum(f.get('size', 0) for f in files.get('files', []))
        print(f"  - Total Size: {total_file_size / (1024*1024):.1f} MB")
        
        # Group files by extension
        file_list = files.get('files', [])
        if file_list:
            ext_sizes = defaultdict(int)
            ext_counts = defaultdict(int)
            
            for f in file_list:
                path = f.get('path', '')
                size = f.get('size', 0)
                ext = os.path.splitext(path)[1] or 'no_extension'
                ext_sizes[ext] += size
                ext_counts[ext] += 1
            
            print("\n  Files by Type:")
            for ext, size in sorted(ext_sizes.items(), key=lambda x: x[1], reverse=True):
                print(f"    • {ext}: {ext_counts[ext]} files, {size / (1024*1024):.1f} MB")
        
        print(f"\nNetwork Connections:")
        print(f"  - Total: {network.get('total_connections', 0)}")
        conn_states = network.get('states', {})
        if conn_states:
            print("  - States:")
            for state, count in conn_states.items():
                print(f"    • {state}: {count}")
        
        print(f"\nThreads:")
        print(f"  - Count: {threads.get('count', 0)}")
    
    # Show memory growth analysis
    if tracemalloc_data:
        latest_tracemalloc = tracemalloc_data[-1]['allocations']
        print("\nPython Object Memory Usage (via tracemalloc):")
        total_size = sum(alloc['size'] for alloc in latest_tracemalloc)
        print(f"Total Tracked Memory: {total_size / (1024*1024):.1f} MB\n")
        
        print("Top Memory Consumers:")
        for i, alloc in enumerate(latest_tracemalloc[:5], 1):
            print(f"- #{i}: {alloc['file']}:{alloc['line']}")
            print(f"  Size: {alloc['size'] / (1024*1024):.2f} MB")
            print(f"  Objects: {alloc['count']:,}")
    
    # Generate performance graphs
    if system_metrics:
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            
            # Create figure with subplots
            fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 12))
            
            # CPU Usage plot
            timestamps = [entry['timestamp'] for entry in system_metrics]
            cpu_usage = [entry['cpu_percent'] for entry in system_metrics]
            ax1.plot(timestamps, cpu_usage, label='CPU Usage %')
            ax1.set_title('CPU Usage Over Time')
            ax1.set_ylabel('CPU %')
            ax1.grid(True)
            ax1.legend()
            
            # Memory Usage plot
            rss_memory = [entry['rss_memory'] for entry in system_metrics]
            vms_memory = [entry['vms_memory'] for entry in system_metrics]
            
            # Extract file-backed memory if available
            file_backed_memory = []
            if anonymous_memory:
                file_backed_memory = [
                    int(entry['file_backed'].get('total_size', 0)) / (1024*1024)  # Convert to MB
                    for entry in anonymous_memory
                ]
            
            ax2.plot(timestamps, rss_memory, label='RSS Memory (MB)', color='blue')
            ax2.plot(timestamps, vms_memory, label='VMS Memory (MB)', color='green')
            if file_backed_memory:
                # Ensure we have matching timestamps for file-backed memory
                fb_timestamps = [entry['timestamp'] for entry in anonymous_memory]
                if len(fb_timestamps) == len(file_backed_memory):
                    ax2.plot(fb_timestamps, file_backed_memory, label='File-Backed Memory (MB)', 
                            color='orange', linestyle='--')
            
            ax2.set_title('Memory Usage Over Time')
            ax2.set_ylabel('Memory (MB)')
            ax2.grid(True)
            ax2.legend()
            
            # Memory Growth Analysis
            if tracemalloc_data:
                print("\nAnalyzing Memory Growth Patterns:")
                # Track top memory consumers over time
                growth_by_file = defaultdict(list)
                growth_timestamps = []
                
                # Initialize the timestamps first
                growth_timestamps = [entry['timestamp'] for entry in tracemalloc_data]
                
                # Initialize all files with zeros for all timestamps
                for entry in tracemalloc_data:
                    for alloc in entry['allocations']:
                        file_key = f"{alloc['file']}:{alloc['line']}"
                        growth_by_file[file_key] = [0] * len(growth_timestamps)
                
                # Now fill in the actual values
                for i, entry in enumerate(tracemalloc_data):
                    for alloc in entry['allocations']:
                        file_key = f"{alloc['file']}:{alloc['line']}"
                        size_mb = alloc['size'] / (1024 * 1024)
                        growth_by_file[file_key][i] = size_mb
                
                # Plot top 5 memory consumers over time
                top_consumers = sorted(
                    growth_by_file.items(),
                    key=lambda x: max(x[1]),
                    reverse=True
                )[:5]
                
                for file_key, sizes in top_consumers:
                    ax3.plot(growth_timestamps, sizes, label=file_key, marker='.')
                
                ax3.set_title('Top Memory Consumers Over Time')
                ax3.set_ylabel('Memory (MB)')
                ax3.grid(True)
                ax3.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
                
                # Print growth analysis
                print("\nMemory Growth Analysis:")
                for file_key, sizes in top_consumers:
                    start_size = sizes[0]
                    end_size = sizes[-1]
                    max_size = max(sizes)
                    growth = end_size - start_size
                    print(f"\n{file_key}:")
                    print(f"  Initial Size: {start_size:.2f} MB")
                    print(f"  Final Size: {end_size:.2f} MB")
                    print(f"  Peak Size: {max_size:.2f} MB")
                    print(f"  Net Growth: {growth:+.2f} MB")
                    
                    # Calculate growth rate
                    if len(sizes) > 1:
                        time_span = (growth_timestamps[-1] - growth_timestamps[0]).total_seconds() / 3600  # hours
                        growth_rate = growth / time_span
                        print(f"  Growth Rate: {growth_rate:+.2f} MB/hour")
            
            # Format x-axis for all plots
            for ax in [ax1, ax2, ax3]:
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
                # Adjust the number of ticks based on the data span
                minutes = (timestamps[-1] - timestamps[0]).total_seconds() / 60
                if minutes > 60:
                    ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=int(minutes/20)))
                else:
                    ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=max(1, int(minutes/10))))
                plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
            
            # Adjust layout to prevent label overlap
            plt.tight_layout(rect=[0, 0, 0.85, 1])  # Make room for legend
            
            # Optional: Add data thinning for very large datasets
            if len(timestamps) > 500:  # If we have more than 500 points
                print("\nNote: Dataset contains many points. Applying data thinning for visualization...")
                thin_factor = len(timestamps) // 500
                for ax in [ax1, ax2, ax3]:
                    for line in ax.lines:
                        line.set_markevery(thin_factor)  # Show markers at intervals
            
            plt.savefig('memory_analysis.png', bbox_inches='tight', dpi=150)
            print("\nGenerated performance graphs in memory_analysis.png")
            
        except ImportError:
            print("matplotlib not installed, skipping graphs")
        except Exception as e:
            print(f"Error generating graphs: {str(e)}")

if __name__ == "__main__":
    log_path = "/user/logs/performance_log.json"
    print(f"Starting analysis of {log_path}")
    
    metrics, anon_mem, net_states, threads, trace_data = parse_performance_log(log_path)
    if not any([metrics, anon_mem, net_states, threads, trace_data]):
        print("No data found in log file")
        sys.exit(1)
        
    analyze_results(metrics, anon_mem, net_states, threads, trace_data)
