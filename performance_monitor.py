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
        """Log detailed memory information including garbage collector stats"""
        try:
            gc.collect()  # Run garbage collection
            
            # Get garbage collector stats
            gc_counts = gc.get_count()
            gc_objects = len(gc.get_objects())
            
            # Get memory by type
            type_sizes = defaultdict(int)
            type_counts = defaultdict(int)
            
            # Track memory by module/file
            file_sizes = defaultdict(int)
            file_objects = defaultdict(int)
            
            # Track memory by function (for function objects)
            function_sizes = defaultdict(int)
            
            for obj in gc.get_objects():
                obj_type = type(obj).__name__
                obj_size = sys.getsizeof(obj)
                type_sizes[obj_type] += obj_size
                type_counts[obj_type] += 1
                
                # Track memory by module/file
                try:
                    if hasattr(obj, '__module__') and obj.__module__:
                        file_sizes[obj.__module__] += obj_size
                        file_objects[obj.__module__] += 1
                except Exception:
                    pass
                
                # Track function memory usage
                try:
                    if callable(obj) and hasattr(obj, '__code__'):
                        func_key = f"{obj.__module__}.{obj.__qualname__}"
                        function_sizes[func_key] += obj_size
                except Exception:
                    pass
            
            # Sort by size and get top entries
            top_types = sorted(type_sizes.items(), key=lambda x: x[1], reverse=True)[:10]
            top_files = sorted(file_sizes.items(), key=lambda x: x[1], reverse=True)[:10]
            top_functions = sorted(function_sizes.items(), key=lambda x: x[1], reverse=True)[:10]
            
            self.performance_logger.info("""
🔍 MEMORY ANALYSIS
-----------------
♻️  Garbage Collector
   Generation Counts: {}
   Total Objects: {:,}

📦 Top Memory Users by Type
{}

📂 Top Memory Users by Module
{}

⚡️ Top Memory Users by Function
{}""".format(
                gc_counts,
                gc_objects,
                '\n'.join(f"   {t[0]:<20} {self._format_size(t[1]):>10} | {type_counts[t[0]]:,} objects" 
                         for t in top_types),
                '\n'.join(f"   {f[0]:<30} {self._format_size(f[1]):>10} | {file_objects[f[0]]:,} objects"
                         for f in top_files),
                '\n'.join(f"   {f[0]:<40} {self._format_size(f[1]):>10}"
                         for f in top_functions)
            ))
            
            # Create log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "detailed_memory",
                "metrics": {
                    "gc_counts": list(gc_counts),
                    "total_objects": gc_objects,
                    "top_types": [{"type": t[0], "size": t[1], "count": type_counts[t[0]]} for t in top_types],
                    "top_files": [{"module": f[0], "size": f[1], "count": file_objects[f[0]]} for f in top_files],
                    "top_functions": [{"function": f[0], "size": f[1]} for f in top_functions]
                }
            }
            
            # Add entry and write JSON
            self.performance_data['entries'].append(log_entry)
            self._write_json()
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
