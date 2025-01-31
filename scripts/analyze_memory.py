#!/usr/bin/env python3
import json
import datetime
import matplotlib.pyplot as plt
import numpy as np
from collections import defaultdict
import sys

def parse_size_to_mb(size_str):
    """Convert size string to MB"""
    try:
        # If it's already a number, convert to MB
        size = float(size_str)
        return size / (1024 * 1024)  # Convert bytes to MB
    except:
        size_str = str(size_str).strip()
        if 'GB' in size_str:
            return float(size_str.replace('GB', '').strip()) * 1024
        elif 'MB' in size_str:
            return float(size_str.replace('MB', '').strip())
        elif 'KB' in size_str:
            return float(size_str.replace('KB', '').strip()) / 1024
        else:
            return 0

def parse_performance_log(log_path):
    memory_data = []
    anonymous_memory = []
    network_states = []
    thread_data = []
    tracemalloc_data = []
    
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            print(f"Reading log file: {log_path}")
            data = json.load(f)
            
            entries = data.get('entries', [])
            print(f"Found {len(entries)} report entries")
            
            for entry in entries:
                timestamp_str = entry.get('timestamp')
                if not timestamp_str:
                    continue
                
                current_time = datetime.datetime.fromisoformat(timestamp_str)
                memory_info = entry.get('memory', {})
                
                # Process anonymous memory
                anon_info = memory_info.get('anonymous', {})
                if anon_info:
                    anonymous_memory.append({
                        'timestamp': current_time,
                        'size_mb': parse_size_to_mb(anon_info.get('total_size', 0)),
                        'count': anon_info.get('count', 0)
                    })
                
                # Process network states
                net_info = memory_info.get('network', {})
                if net_info:
                    network_states.append({
                        'timestamp': current_time,
                        'total': net_info.get('total_connections', 0),
                        'states': net_info.get('states', {})
                    })
                
                # Process thread data
                thread_info = memory_info.get('threads', {})
                if thread_info:
                    thread_data.append({
                        'timestamp': current_time,
                        'count': thread_info.get('count', 0),
                        'stats': thread_info.get('stats', [])
                    })
                
                # Process tracemalloc data
                trace_info = memory_info.get('tracemalloc', {})
                if trace_info:
                    tracemalloc_data.append({
                        'timestamp': current_time,
                        'allocations': trace_info.get('top_allocations', [])
                    })
        
        print(f"Successfully parsed:")
        print(f"- {len(anonymous_memory)} anonymous memory entries")
        print(f"- {len(network_states)} network state entries")
        print(f"- {len(thread_data)} thread data entries")
        print(f"- {len(tracemalloc_data)} tracemalloc entries")
        
        return anonymous_memory, network_states, thread_data, tracemalloc_data
    
    except Exception as e:
        print(f"Error reading log file: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return [], [], [], []

def analyze_memory_patterns(anonymous_memory, network_states, thread_data, tracemalloc_data):
    if not anonymous_memory:
        return "No data found to analyze."
    
    report = []
    report.append("Memory Usage Analysis Report")
    report.append("=" * 30)
    
    # Analyze Anonymous Memory
    report.append("\n📊 Anonymous Memory Analysis")
    report.append("-" * 25)
    
    anon_sizes = [entry['size_mb'] for entry in anonymous_memory]
    anon_counts = [entry['count'] for entry in anonymous_memory]
    
    report.append(f"Initial Size: {anon_sizes[0]:.1f} MB")
    report.append(f"Final Size: {anon_sizes[-1]:.1f} MB")
    report.append(f"Peak Size: {max(anon_sizes):.1f} MB")
    report.append(f"Average Size: {np.mean(anon_sizes):.1f} MB")
    report.append(f"Size Standard Deviation: {np.std(anon_sizes):.1f} MB")
    report.append(f"Number of Anonymous Mappings: {anon_counts[-1]}")
    
    # Analyze Network Connections
    report.append("\n🌐 Network Connection Analysis")
    report.append("-" * 25)
    
    if network_states:
        latest_states = network_states[-1]['states']
        report.append(f"Current Total Connections: {network_states[-1]['total']}")
        report.append("\nConnection States:")
        for state, count in latest_states.items():
            report.append(f"  {state}: {count}")
        
        # Track CLOSE_WAIT trends
        close_wait_counts = [entry['states'].get('CLOSE_WAIT', 0) for entry in network_states]
        if any(close_wait_counts):
            report.append(f"\nCLOSE_WAIT Connections:")
            report.append(f"  Current: {close_wait_counts[-1]}")
            report.append(f"  Peak: {max(close_wait_counts)}")
            report.append(f"  Average: {np.mean(close_wait_counts):.1f}")
    
    # Analyze Thread Usage
    report.append("\n🧵 Thread Analysis")
    report.append("-" * 25)
    
    if thread_data:
        thread_counts = [entry['count'] for entry in thread_data]
        latest_threads = thread_data[-1]['stats']
        
        report.append(f"Current Thread Count: {thread_counts[-1]}")
        report.append(f"Peak Thread Count: {max(thread_counts)}")
        report.append(f"Average Thread Count: {np.mean(thread_counts):.1f}")
        
        # Analyze busy threads
        busy_threads = [
            thread for thread in latest_threads
            if thread['user_time'] > 0 or thread['system_time'] > 0
        ]
        report.append(f"\nBusy Threads: {len(busy_threads)}")
        
        # Sort threads by total CPU time
        sorted_threads = sorted(
            busy_threads,
            key=lambda x: x['user_time'] + x['system_time'],
            reverse=True
        )
        
        report.append("\nTop CPU-consuming threads:")
        for thread in sorted_threads[:5]:
            total_time = thread['user_time'] + thread['system_time']
            report.append(f"  Thread {thread['id']}: {total_time:.1f}s total CPU time")
    
    # Analyze Memory Allocations
    report.append("\n📍 Top Memory Allocation Sites")
    report.append("-" * 25)
    
    if tracemalloc_data:
        latest_traces = tracemalloc_data[-1]['allocations']
        for alloc in latest_traces[:5]:
            size_mb = parse_size_to_mb(alloc['size'])
            report.append(f"  {alloc['file']}:{alloc['line']}")
            report.append(f"    Size: {size_mb:.1f} MB")
    
    # Create visualizations
    plt.figure(figsize=(15, 12))
    
    # Plot anonymous memory
    plt.subplot(3, 1, 1)
    timestamps = [entry['timestamp'] for entry in anonymous_memory]
    plt.plot(timestamps, anon_sizes, 'b-', label='Anonymous Memory')
    plt.title('Anonymous Memory Usage Over Time')
    plt.ylabel('Memory (MB)')
    plt.legend()
    
    # Plot network connections
    plt.subplot(3, 1, 2)
    timestamps = [entry['timestamp'] for entry in network_states]
    total_connections = [entry['total'] for entry in network_states]
    plt.plot(timestamps, total_connections, 'g-', label='Total Connections')
    
    # Add CLOSE_WAIT if present
    close_wait_counts = [entry['states'].get('CLOSE_WAIT', 0) for entry in network_states]
    if any(close_wait_counts):
        plt.plot(timestamps, close_wait_counts, 'r-', label='CLOSE_WAIT')
    
    plt.title('Network Connections Over Time')
    plt.ylabel('Count')
    plt.legend()
    
    # Plot thread count
    plt.subplot(3, 1, 3)
    timestamps = [entry['timestamp'] for entry in thread_data]
    thread_counts = [entry['count'] for entry in thread_data]
    plt.plot(timestamps, thread_counts, 'm-', label='Thread Count')
    plt.title('Thread Count Over Time')
    plt.ylabel('Count')
    plt.legend()
    
    plt.tight_layout()
    plt.savefig('memory_analysis.png')
    
    return "\n".join(report)

if __name__ == "__main__":
    log_path = "/user/logs/performance_log.json"
    print(f"Starting analysis of {log_path}")
    
    anonymous_memory, network_states, thread_data, tracemalloc_data = parse_performance_log(log_path)
    report = analyze_memory_patterns(anonymous_memory, network_states, thread_data, tracemalloc_data)
    
    print("\nAnalysis Report:")
    print(report)
    print("\nVisualization saved as 'memory_analysis.png'")
