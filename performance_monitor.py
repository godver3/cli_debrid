import psutil
import os
import gc
import sys
import logging
import threading
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
import time
import json
import platform
from settings import get_setting

class PerformanceMonitor:
    """Monitor system performance metrics"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """Initialize the performance monitor"""
        # Only initialize once
        if hasattr(self, 'initialized'):
            return
        self.initialized = True
        
        # Initialize performance logger
        self.performance_logger = logging.getLogger('performance_logger')
        self.performance_logger.propagate = False  # Don't propagate to root logger
        
        # Store CPU metrics
        self.cpu_times = defaultdict(list)  # Store CPU times per process
        self.cpu_percent_history = defaultdict(list)  # Store CPU % history
        self.process = psutil.Process()
        self.last_cpu_measure_time = None
        self.cpu_measure_interval = 1.0  # seconds
        
        # Store memory snapshots (timestamp, snapshot)
        self.memory_snapshots = []
        
        # Store last memory info for delta calculation
        self._last_memory_info = None
        
        # Initialize JSON logger
        self.json_logger = logging.getLogger('json_performance_logger')
        self.json_logger.propagate = False
        log_dir = os.environ.get('USER_LOGS', '/user/logs')
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        self.log_file = os.path.join(log_dir, 'performance_log.json')
        
        # Maximum number of entries to keep in the log file
        self.max_entries = 1440  # 24 hours worth of entries at 1 per minute
        
        # Polling intervals (in seconds)
        self.basic_metrics_interval = 15  # Poll basic metrics every 15 seconds
        self.detailed_metrics_interval = 60  # Poll detailed metrics every minute
        self.snapshot_interval = 1800  # Take memory snapshots every 30 minutes
        self.log_cleanup_interval = 3600  # Clean up logs every hour
        
        # Timestamps for last operations
        self.last_detailed_check = datetime.now()
        self.last_snapshot = datetime.now()
        self.last_cleanup = datetime.now()
        
        # Write initial metadata
        self._write_metadata()
    
    def start_monitoring(self):
        """Start comprehensive performance monitoring"""
        if not hasattr(self, 'monitor_thread') or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            self.performance_logger.info("Started performance monitoring")
    
    def stop_monitoring(self):
        """Stop performance monitoring"""
        try:
            pass
        except Exception as e:
            self.performance_logger.error(f"Error stopping performance monitoring: {e}")
    
    def _monitor_loop(self):
        """Main monitoring loop that collects various performance metrics"""
        while True:
            try:
                # Always collect basic metrics at higher frequency
                self._log_basic_metrics()
                
                current_time = datetime.now()
                
                # Check if it's time for detailed metrics
                if (current_time - self.last_detailed_check).seconds >= self.detailed_metrics_interval:
                    self.performance_logger.info("\n" + "=" * 100)
                    self.performance_logger.info(" " * 40 + "DETAILED PERFORMANCE REPORT" + " " * 40)
                    self.performance_logger.info("=" * 100 + "\n")
                    
                    self._log_detailed_memory()
                    self.performance_logger.info("\n" + "-" * 100 + "\n")
                    
                    self._log_memory_growth()
                    self.performance_logger.info("\n" + "-" * 100 + "\n")
                    
                    self._log_file_descriptors()
                    self.performance_logger.info("\n" + "-" * 100 + "\n")
                    
                    self._log_cpu_metrics()
                    self.performance_logger.info("\n" + "-" * 100 + "\n")
                    
                    self.last_detailed_check = current_time
                
                # Check if it's time for a memory snapshot
                if (current_time - self.last_snapshot).seconds >= self.snapshot_interval:
                    self._take_memory_snapshot()
                    self.last_snapshot = current_time

                # Check if it's time to clean up old log entries
                if (current_time - self.last_cleanup).seconds >= self.log_cleanup_interval:
                    self._cleanup_old_logs()
                    self.last_cleanup = current_time
                
            except Exception as e:
                self.performance_logger.error(f"Error in performance monitoring: {str(e)}")
            
            time.sleep(self.basic_metrics_interval)  # Sleep for basic metrics interval
    
    def _format_size(self, size_bytes):
        """Format size in bytes to human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"
    
    def _write_metadata(self):
        """Write metadata to the log file"""
        # Just ensure the file exists and is empty
        with open(self.log_file, 'w') as f:
            pass
    
    def _write_entry(self, entry):
        """Append a single entry to the log file"""
        try:
            # Append entry directly without reading the file
            with open(self.log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
                
        except Exception as e:
            self.performance_logger.error(f"Error writing entry: {e}")
    
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
            
            # Calculate actual memory usage percentage without cache
            total_memory = virtual_memory.total
            used_memory = virtual_memory.total - virtual_memory.available
            memory_percent = (used_memory / total_memory) * 100
            
            memory_delta_str = ""
            if self._last_memory_info is not None:
                try:
                    memory_delta = (memory_info.rss - self._last_memory_info) / 1024 / 1024
                    memory_delta_str = f" ({'+' if memory_delta >= 0 else ''}{memory_delta:.1f} MB since last check)"
                except Exception:
                    memory_delta_str = ""
            
            self._last_memory_info = memory_info.rss
            
            # Create entry with UTC timestamp
            current_time = datetime.utcnow()
            entry = {
                "timestamp": current_time.isoformat(),
                "type": "basic_metrics",
                "metrics": {
                    "cpu_percent": cpu_percent,
                    "memory_rss": memory_info.rss / 1024 / 1024,  # Convert to MB
                    "memory_vms": memory_info.vms / 1024 / 1024,  # Convert to MB
                    "system_memory_used": memory_percent,
                    "swap_used": psutil.swap_memory().used / 1024 / 1024,  # Convert to MB
                    "cpu_user_time": cpu_times.user,
                    "cpu_system_time": cpu_times.system
                }
            }
            
            # Write entry to log file
            self._write_entry(entry)
            
            # Log human-readable format
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
                memory_percent,
                psutil.swap_memory().used / 1024 / 1024
            ))
            
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
            
            # Write entry to log file
            self._write_entry(entry)
            
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
            
        except Exception as e:
            self.performance_logger.error(f"Error logging detailed memory: {e}")
    
    def _log_memory_growth(self):
        """Log memory growth"""
        try:
            self.performance_logger.info("""
📈 MEMORY GROWTH
---------------
No memory growth data available""")
            
            # Create log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "memory_growth",
                "metrics": {
                    "message": "No memory growth data available"
                }
            }
            
            # Write entry to log file
            self._write_entry(log_entry)
            
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
            
            # Write entry to log file
            self._write_entry(log_entry)
            
        except Exception as e:
            self.performance_logger.error(f"Error logging file descriptors: {e}")
    
    def _log_cpu_metrics(self):
        """Log CPU usage metrics without full profiling"""
        try:
            current_time = time.time()
            
            # Only measure if enough time has passed
            if (self.last_cpu_measure_time is None or 
                current_time - self.last_cpu_measure_time >= self.cpu_measure_interval):
                
                # Get process CPU times
                cpu_times = self.process.cpu_times()
                self.cpu_times['user'].append(cpu_times.user)
                self.cpu_times['system'].append(cpu_times.system)
                
                # Get CPU percentage for process (non-blocking)
                cpu_percent = self.process.cpu_percent(interval=None)
                self.cpu_percent_history['process'].append(cpu_percent)
                
                # Get per-thread CPU times
                thread_times = []
                for thread in self.process.threads():
                    thread_times.append({
                        'id': thread.id,
                        'user_time': thread.user_time,
                        'system_time': thread.system_time
                    })
                
                # Keep only last 60 measurements (1 hour at 1 min intervals)
                max_history = 60
                self.cpu_times['user'] = self.cpu_times['user'][-max_history:]
                self.cpu_times['system'] = self.cpu_times['system'][-max_history:]
                self.cpu_percent_history['process'] = self.cpu_percent_history['process'][-max_history:]
                
                # Calculate CPU usage deltas
                user_delta = self.cpu_times['user'][-1] - self.cpu_times['user'][0] if len(self.cpu_times['user']) > 1 else 0
                system_delta = self.cpu_times['system'][-1] - self.cpu_times['system'][0] if len(self.cpu_times['system']) > 1 else 0
                
                # Log CPU metrics
                self.performance_logger.info(f"""
CPU Usage Metrics:
----------------
Process CPU: {cpu_percent:.1f}%
User Time Δ: {user_delta:.2f}s
System Time Δ: {system_delta:.2f}s
Active Threads: {len(thread_times)}
Top Thread Usage:
{self._format_thread_times(thread_times[:5])}
""")

                # Create JSON log entry with CPU profiling information
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "type": "cpu_metrics",
                    "metrics": {
                        "process_cpu_percent": cpu_percent,
                        "user_time_delta": user_delta,
                        "system_time_delta": system_delta,
                        "active_threads": len(thread_times),
                        "cpu_times": {
                            "user": cpu_times.user,
                            "system": cpu_times.system,
                            "children_user": getattr(cpu_times, 'children_user', 0),
                            "children_system": getattr(cpu_times, 'children_system', 0)
                        },
                        "thread_times": sorted(
                            thread_times,
                            key=lambda x: x['user_time'] + x['system_time'],
                            reverse=True
                        )[:5]  # Include only top 5 threads
                    }
                }
                
                # Write entry to log file
                self._write_entry(log_entry)
                
                # Update measurement time
                self.last_cpu_measure_time = current_time
                
        except Exception as e:
            self.performance_logger.error(f"Error logging CPU metrics: {e}")

    def _format_thread_times(self, thread_times):
        """Format thread times for logging"""
        return '\n'.join(
            f"  Thread {t['id']}: {t['user_time']:.2f}s user, {t['system_time']:.2f}s system"
            for t in sorted(thread_times, key=lambda x: x['user_time'] + x['system_time'], reverse=True)
        )
    
    def _take_memory_snapshot(self):
        """Take a memory snapshot"""
        try:
            self.performance_logger.info("""
📈 MEMORY SNAPSHOT
---------------
No memory snapshot available""")
            
            # Create log entry
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "memory_snapshot",
                "metrics": {
                    "message": "No memory snapshot available"
                }
            }
            
            # Write entry to log file
            self._write_entry(log_entry)
            
        except Exception as e:
            self.performance_logger.error(f"Error taking memory snapshot: {e}")

    def _cleanup_old_logs(self):
        """Clean up old log entries to prevent file growth"""
        try:
            # Keep last 24 hours of entries (one entry per minute = 1440 entries)
            max_age = 24 * 60 * 60  # 24 hours in seconds
            cutoff_time = time.time() - max_age
            
            if not os.path.exists(self.log_file):
                return
                
            # Create a temporary file
            temp_file = self.log_file + '.temp'
            kept_count = 0
            removed_count = 0
            
            with open(self.log_file, 'r') as old_file, open(temp_file, 'w') as new_file:
                for line in old_file:
                    try:
                        entry = json.loads(line.strip())
                        entry_time = datetime.fromisoformat(entry['timestamp']).timestamp()
                        
                        if entry_time >= cutoff_time:
                            new_file.write(line)
                            kept_count += 1
                        else:
                            removed_count += 1
                    except (json.JSONDecodeError, KeyError, ValueError):
                        # Keep lines we can't parse, just in case
                        new_file.write(line)
                        kept_count += 1
            
            # Replace old file with new file
            os.replace(temp_file, self.log_file)
            
            self.performance_logger.info(f"Cleaned up performance logs: kept {kept_count} entries, removed {removed_count} entries")
            
        except Exception as e:
            self.performance_logger.error(f"Error cleaning up old log entries: {e}")
            # If cleanup fails, don't leave temp file behind
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass

# Create singleton instance but don't start monitoring yet
monitor = PerformanceMonitor()

def start_performance_monitoring():
    """Start performance monitoring after app initialization"""
    monitor.start_monitoring()
