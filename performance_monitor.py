import psutil
import os
import gc
import sys
import tracemalloc
import logging
import threading
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
import time
import cProfile
import pstats
import io
import re
import json
import platform

class PerformanceMonitor:
    def __init__(self):
        """Initialize the performance monitor"""
        # Initialize performance logger
        self.performance_logger = logging.getLogger('performance_logger')
        self.performance_logger.propagate = False  # Don't propagate to root logger
        
        # Initialize tracemalloc if not already started
        self.tracemalloc_enabled = False
        try:
            if not tracemalloc.is_tracing():
                tracemalloc.start()
                self.tracemalloc_enabled = True
        except Exception as e:
            self.performance_logger.error(f"Failed to start tracemalloc: {e}")
        
        # Initialize profiler
        self.profiler = cProfile.Profile()
        self.profiler.enable()
        
        # Store memory snapshots (timestamp, snapshot)
        self.memory_snapshots = []
        
        # Store last memory info for delta calculation
        self._last_memory_info = None
        
        # Start monitoring in a separate thread
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        # Initialize JSON logger
        self.json_logger = logging.getLogger('json_performance_logger')
        self.json_logger.propagate = False
        log_dir = os.environ.get('USER_LOGS', '/user/logs')
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        self.log_file = os.path.join(log_dir, 'performance_log.json')
        
        # Maximum number of entries to keep in the log
        self.max_entries = 1440  # 24 hours worth of entries at 1 per minute
        
        # Initialize with empty structured data
        self.performance_data = {
            "metadata": {
                "start_time": datetime.now().isoformat(),
                "system": platform.system(),
                "python_version": platform.python_version()
            },
            "entries": []
        }
        
        # Load existing data if available
        self._load_json()
        
        # Write initial empty structure
        self._write_json()
    
    def start_monitoring(self):
        """Start comprehensive performance monitoring"""
        try:
            if not self.tracemalloc_enabled:
                tracemalloc.start()
                self.tracemalloc_enabled = True
            self._setup_profiler()
            if not self.monitor_thread.is_alive():
                self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
                self.monitor_thread.start()
        except Exception as e:
            self.performance_logger.error(f"Error starting performance monitoring: {e}")
    
    def stop_monitoring(self):
        """Stop performance monitoring"""
        try:
            if self.tracemalloc_enabled:
                tracemalloc.stop()
                self.tracemalloc_enabled = False
            if hasattr(self, 'profiler'):
                self.profiler.disable()
        except Exception as e:
            self.performance_logger.error(f"Error stopping performance monitoring: {e}")
    
    def _monitor_loop(self):
        """Main monitoring loop that collects various performance metrics"""
        while True:
            try:
                self.performance_logger.info("\n")  # Start with a blank line
                self.performance_logger.info("=" * 100)
                self.performance_logger.info(" " * 40 + "PERFORMANCE REPORT" + " " * 40)
                self.performance_logger.info("=" * 100 + "\n")
                
                self._log_basic_metrics()
                self.performance_logger.info("\n" + "-" * 100 + "\n")
                
                self._log_detailed_memory()
                self.performance_logger.info("\n" + "-" * 100 + "\n")
                
                self._log_memory_growth()
                self.performance_logger.info("\n" + "-" * 100 + "\n")
                
                self._log_file_descriptors()
                self.performance_logger.info("\n" + "-" * 100 + "\n")
                
                self._log_cpu_profile()
                self.performance_logger.info("\n" + "=" * 100 + "\n")
                
                # Take memory snapshot every 30 minutes
                if len(self.memory_snapshots) == 0 or \
                   (datetime.now() - self.memory_snapshots[-1][0]).seconds > 1800:
                    self._take_memory_snapshot()
                
            except Exception as e:
                self.performance_logger.error(f"Error in performance monitoring: {str(e)}")
            
            time.sleep(60)  # Run every minute
    
    def _format_size(self, size_bytes):
        """Format size in bytes to human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"
    
    def _write_json(self):
        """Write the current performance data to JSON file"""
        try:
            # Keep only the most recent entries
            if len(self.performance_data["entries"]) > self.max_entries:
                self.performance_data["entries"] = self.performance_data["entries"][-self.max_entries:]
            
            with open(self.log_file, 'w') as f:
                json.dump(self.performance_data, f, indent=2)
        except Exception as e:
            logging.error(f"Error writing performance JSON: {e}")
    
    def _load_json(self):
        """Load existing performance data from JSON file"""
        try:
            if os.path.exists(self.log_file):
                with open(self.log_file, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "entries" in data:
                        self.performance_data = data
        except Exception as e:
            logging.error(f"Error loading performance JSON: {e}")
    
    def _log_basic_metrics(self):
        """Log basic CPU and memory metrics"""
        try:
            process = psutil.Process(os.getpid())
            
            # Get CPU info with longer interval for more accurate measurement
            cpu_percent = process.cpu_percent(interval=2)
            cpu_times = process.cpu_times()
            
            # Get memory info with deltas
            memory_info = process.memory_info()
            virtual_memory = psutil.virtual_memory()
            
            memory_delta_str = ""
            if self._last_memory_info is not None:
                try:
                    memory_delta = (memory_info.rss - self._last_memory_info) / 1024 / 1024
                    memory_delta_str = f" ({'+' if memory_delta >= 0 else ''}{memory_delta:.1f} MB since last check)"
                except Exception:
                    memory_delta_str = ""
            
            self._last_memory_info = memory_info.rss
            
            self.performance_logger.info(f"Current time: {datetime.now():%Y-%m-%d %H:%M:%S}")

            self.performance_logger.info("""
📊 SYSTEM RESOURCES
------------------
🔲 CPU
   Current Usage: {:>5.1f}%
   User Time:    {:>5.1f}s
   System Time:  {:>5.1f}s

💾 MEMORY
   RSS Memory:   {:>5.1f} MB{}
   VMS Memory:   {:>5.1f} MB
   System Used:  {:>5.1f}%
   Swap Used:    {:>5.1f} MB""".format(
                cpu_percent,
                cpu_times.user,
                cpu_times.system,
                memory_info.rss / 1024 / 1024,
                memory_delta_str,
                memory_info.vms / 1024 / 1024,
                virtual_memory.percent,
                psutil.swap_memory().used / 1024 / 1024
            ))
            
            # Create log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "basic_metrics",
                "metrics": {
                    "cpu_percent": cpu_percent,
                    "cpu_user_time": cpu_times.user,
                    "cpu_system_time": cpu_times.system,
                    "memory_rss": memory_info.rss / 1024 / 1024,
                    "memory_vms": memory_info.vms / 1024 / 1024,
                    "system_memory_used": virtual_memory.percent,
                    "swap_used": psutil.swap_memory().used / 1024 / 1024
                }
            }
            
            # Add entry and write JSON
            self.performance_data['entries'].append(log_entry)
            self._write_json()
        except Exception as e:
            self.performance_logger.error(f"Error logging basic metrics: {e}")
    
    def _log_detailed_memory(self):
        """Log detailed memory metrics"""
        try:
            process = psutil.Process(os.getpid())
            
            # Get memory maps
            memory_maps = process.memory_maps(grouped=True)
            anon_maps = [m for m in memory_maps if not m.path]
            file_maps = [m for m in memory_maps if m.path]
            
            # Calculate total sizes
            total_anon = sum(int(m.rss) for m in anon_maps)
            total_file = sum(int(m.rss) for m in file_maps)
            
            # Get open files
            open_files = process.open_files()
            file_sizes = defaultdict(int)
            for f in open_files:
                try:
                    file_sizes[f.path] = os.path.getsize(f.path)
                except (OSError, IOError):
                    pass
            
            # Get network connections
            connections = process.connections()
            conn_states = defaultdict(int)
            for conn in connections:
                conn_states[conn.status] += 1
            
            # Get thread information
            threads = process.threads()
            thread_stats = []
            for thread in threads:
                thread_stats.append({
                    'id': thread.id,
                    'user_time': thread.user_time,
                    'system_time': thread.system_time
                })
            
            # Get tracemalloc statistics if enabled
            if self.tracemalloc_enabled:
                snapshot = tracemalloc.take_snapshot()
                stats = snapshot.statistics('lineno')
                top_stats = stats[:10]  # Get top 10 memory allocations
                
                trace_stats = []
                for stat in top_stats:
                    frame = stat.traceback[0]
                    trace_stats.append({
                        'file': frame.filename,
                        'line': frame.lineno,
                        'size': stat.size
                    })
            
            # Create entry for JSON log
            entry = {
                'timestamp': datetime.now().isoformat(),
                'memory': {
                    'anonymous': {
                        'total_size': total_anon,
                        'count': len(anon_maps),
                        'formatted_size': self._format_size(total_anon)
                    },
                    'file_backed': {
                        'total_size': total_file,
                        'count': len(file_maps),
                        'formatted_size': self._format_size(total_file)
                    },
                    'open_files': {
                        'count': len(open_files),
                        'total_size': sum(file_sizes.values()),
                        'files': [{'path': k, 'size': v} for k, v in file_sizes.items()]
                    },
                    'network': {
                        'total_connections': len(connections),
                        'states': dict(conn_states)
                    },
                    'threads': {
                        'count': len(threads),
                        'stats': thread_stats
                    }
                }
            }
            
            if self.tracemalloc_enabled:
                entry['memory']['tracemalloc'] = {
                    'top_allocations': trace_stats
                }
            
            # Add to performance data
            self.performance_data['entries'].append(entry)
            self._write_json()
            
            # Log to performance logger
            self.performance_logger.info("""
💾 DETAILED MEMORY ANALYSIS
-------------------------
Anonymous Memory:
    Total Size: {}
    Number of Mappings: {}

File-backed Memory:
    Total Size: {}
    Number of Mappings: {}

Open Files: {}
    Total Size: {}

Network Connections:
    Total: {}
    States: {}

Threads:
    Count: {}
    Active: {}""".format(
                self._format_size(total_anon),
                len(anon_maps),
                self._format_size(total_file),
                len(file_maps),
                len(open_files),
                self._format_size(sum(file_sizes.values())),
                len(connections),
                dict(conn_states),
                len(threads),
                sum(1 for t in threads if t.user_time > 0 or t.system_time > 0)
            ))
            
            if self.tracemalloc_enabled:
                self.performance_logger.info("\nTop Memory Allocations:")
                for stat in trace_stats:
                    self.performance_logger.info(f"  {stat['file']}:{stat['line']} - {self._format_size(stat['size'])}")
            
        except Exception as e:
            self.performance_logger.error(f"Error logging detailed memory: {e}")
    
    def _log_memory_growth(self):
        """Log memory growth using tracemalloc"""
        try:
            if not self.tracemalloc_enabled:
                return
                
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics('lineno')
            
            self.performance_logger.info("""
📈 MEMORY GROWTH
---------------
Top Memory Allocations by Location:
{}""".format(
                '\n'.join(f"   {stat.count:,} objects: {self._format_size(stat.size)} - {os.path.basename(stat.traceback[0].filename)}:{stat.traceback[0].lineno}"
                         for stat in top_stats[:10])
            ))
            
            # Create log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "memory_growth",
                "metrics": {
                    "top_allocations": [
                        {
                            "count": stat.count,
                            "size": stat.size,
                            "file": os.path.basename(stat.traceback[0].filename),
                            "line": stat.traceback[0].lineno
                        } for stat in top_stats[:10]
                    ]
                }
            }
            
            # Add entry and write JSON
            self.performance_data['entries'].append(log_entry)
            self._write_json()
        except Exception as e:
            self.performance_logger.error(f"Error logging memory growth: {e}")
    
    def _log_file_descriptors(self):
        """Log information about open file descriptors"""
        try:
            process = psutil.Process(os.getpid())
            open_files = process.open_files()
            open_connections = process.connections()
            
            file_types = defaultdict(int)
            for f in open_files:
                ext = os.path.splitext(f.path)[1] or 'no_extension'
                file_types[ext] += 1
            
            conn_status = defaultdict(int)
            for conn in open_connections:
                conn_status[conn.status] += 1
            
            self.performance_logger.info("""
🔌 RESOURCE HANDLES
-----------------
📄 Open Files: {}
   By Type: {}

🌐 Network Connections: {}
   By Status: {}""".format(
                len(open_files),
                ', '.join(f"{ext}: {count}" for ext, count in file_types.items()),
                len(open_connections),
                ', '.join(f"{status}: {count}" for status, count in conn_status.items()) if conn_status else "None"
            ))
            
            # Create log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "file_descriptors",
                "metrics": {
                    "open_files_count": len(open_files),
                    "file_types": {ext: count for ext, count in file_types.items()},
                    "network_connections_count": len(open_connections),
                    "connection_statuses": {str(status): count for status, count in conn_status.items()}
                }
            }
            
            # Add entry and write JSON
            self.performance_data['entries'].append(log_entry)
            self._write_json()
        except Exception as e:
            self.performance_logger.error(f"Error logging file descriptors: {e}")
    
    def _log_cpu_profile(self):
        """Log CPU profiling information"""
        try:
            s = io.StringIO()
            stats = pstats.Stats(self.profiler, stream=s)
            
            total_time = 0
            sleep_time = 0
            
            try:
                total_time = sum(stat[3] for stat in stats.stats.values())
                sleep_time = sum(stat[3] for key, stat in stats.stats.items() 
                               if 'sleep' in str(key[2]).lower())
            except Exception:
                pass
            
            active_time = total_time - sleep_time
            
            # Group stats by file
            file_stats = defaultdict(lambda: {'calls': 0, 'time': 0.0, 'funcs': defaultdict(float)})
            
            for (file, line, func), stat in stats.stats.items():
                if 'sleep' in str(func).lower():
                    continue
                
                func_time = stat[3]
                calls = stat[0]
                
                file_stats[file]['calls'] += calls
                file_stats[file]['time'] += func_time
                file_stats[file]['funcs'][func] += func_time
            
            sorted_files = sorted(file_stats.items(), key=lambda x: x[1]['time'], reverse=True)
            
            # Format output
            output = ["""
⚡️ CPU PROFILE
------------
⏱️  Time Distribution:
   Total Time: {:.1f}s
   Active Time: {:.1f}s
   Idle Time: {:.1f}s

📊 Top Files by CPU Usage:""".format(total_time, active_time, sleep_time)]
            
            for file, data in sorted_files[:10]:
                if file == '~':
                    continue
                file_name = os.path.basename(str(file)) if file else "Unknown"
                output.append(f"   {file_name:<30} {data['time']:>6.1f}s | {data['calls']:,} calls")
                
                sorted_funcs = sorted(data['funcs'].items(), key=lambda x: x[1], reverse=True)
                for func, time in sorted_funcs[:3]:
                    output.append(f"      ↳ {func:<40} {time:>6.1f}s")
                output.append("")
            
            self.performance_logger.info('\n'.join(output))
            
            self._setup_profiler()
            
            # Create log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "cpu_profile",
                "metrics": {
                    "total_time": total_time,
                    "active_time": active_time,
                    "sleep_time": sleep_time,
                    "top_files": [
                        {
                            "file": os.path.basename(str(file)) if file else "Unknown",
                            "time": data['time'],
                            "calls": data['calls'],
                            "top_functions": [
                                {"function": func, "time": time}
                                for func, time in sorted(data['funcs'].items(), key=lambda x: x[1], reverse=True)[:3]
                            ]
                        } for file, data in sorted_files[:10] if file != '~'
                    ]
                }
            }
            
            # Add entry and write JSON
            self.performance_data['entries'].append(log_entry)
            self._write_json()
            
        except Exception as e:
            self.performance_logger.error(f"Error in CPU profiling: {e}")
            self._setup_profiler()
    
    def _setup_profiler(self):
        """Setup or reset the profiler"""
        if hasattr(self, 'profiler'):
            self.profiler.disable()
        self.profiler = cProfile.Profile()
        self.profiler.enable()
    
    def _take_memory_snapshot(self):
        """Take a memory snapshot for leak detection"""
        try:
            if not self.tracemalloc_enabled:
                return
                
            snapshot = tracemalloc.take_snapshot()
            self.memory_snapshots.append((datetime.now(), snapshot))
            
            # Keep only last 24 hours of snapshots
            cutoff_time = datetime.now() - timedelta(hours=24)
            self.memory_snapshots = [(t, s) for t, s in self.memory_snapshots if t > cutoff_time]
            
            # Compare with previous snapshot if available
            if len(self.memory_snapshots) >= 2:
                old_snapshot = self.memory_snapshots[-2][1]
                new_snapshot = self.memory_snapshots[-1][1]
                diff_stats = new_snapshot.compare_to(old_snapshot, 'lineno')
                
                self.performance_logger.info("""
📈 MEMORY GROWTH
---------------
Memory Growth Since Last Snapshot (Top 10):
{}""".format(
                    '\n'.join(f"   {stat}" for stat in diff_stats[:10])
                ))
                
                # Create log entry
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "type": "memory_snapshot",
                    "metrics": {
                        "memory_growth": [
                            {
                                "size": stat.size,
                                "count": stat.count,
                                "traceback": str(stat.traceback)
                            } for stat in diff_stats[:10]
                        ]
                    }
                }
                
                # Add entry and write JSON
                self.performance_data['entries'].append(log_entry)
                self._write_json()
        except Exception as e:
            self.performance_logger.error(f"Error taking memory snapshot: {e}")

# Global instance
monitor = PerformanceMonitor()
