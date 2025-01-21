import os
import re
from collections import defaultdict
from typing import Dict, List, Tuple
import logging
from pathlib import Path

class LogAnalyzer:
    def __init__(self, log_dir: str = '/user/logs'):
        self.log_dir = Path(log_dir)
        self.log_pattern = re.compile(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2},\d{3})\s-\s([^:]+):([^:]+):(\d+)\s-\s(\w+)\s-\s(.+)')
        self.function_stats = defaultdict(lambda: {'count': 0, 'bytes': 0, 'levels': defaultdict(int)})
        self.total_lines = 0
        self.total_bytes = 0
        self.skipped_lines = 0

    def process_log_file(self, log_file: Path) -> None:
        """Process a single log file and update statistics."""
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    self.total_lines += 1
                    self.total_bytes += len(line.encode('utf-8'))
                    
                    match = self.log_pattern.match(line)
                    if match:
                        _, filename, funcname, _, level, message = match.groups()
                        key = f"{filename}:{funcname}"
                        
                        stats = self.function_stats[key]
                        stats['count'] += 1
                        stats['bytes'] += len(line.encode('utf-8'))
                        stats['levels'][level.lower()] += 1
                    else:
                        self.skipped_lines += 1
        except Exception as e:
            logging.error(f"Error processing {log_file}: {str(e)}")

    def analyze_logs(self) -> None:
        """Process all debug log files in the directory."""
        for i in range(6):  # Process debug.log through debug.log.5
            suffix = '' if i == 0 else f'.{i}'
            log_file = self.log_dir / f'debug.log{suffix}'
            if log_file.exists():
                self.process_log_file(log_file)

    def get_top_functions(self, limit: int = 20) -> List[Tuple[str, Dict]]:
        """Get the top N functions by log entry count."""
        sorted_funcs = sorted(
            self.function_stats.items(),
            key=lambda x: x[1]['count'],
            reverse=True
        )
        return sorted_funcs[:limit]

    def print_analysis(self) -> None:
        """Print the analysis results."""
        print("\n=== Log Analysis Report ===")
        print(f"\nTotal lines processed: {self.total_lines}")
        print(f"Total bytes processed: {self.total_bytes:,} bytes")
        print(f"Skipped lines: {self.skipped_lines}")
        
        print("\nTop 20 Functions by Log Entry Count:")
        print("-" * 80)
        print(f"{'Function':<40} {'Count':<10} {'Size':<15} {'% of Total':<12} {'Log Levels'}")
        print("-" * 80)
        
        for func_name, stats in self.get_top_functions(20):
            count = stats['count']
            bytes_size = stats['bytes']
            percentage = (count / self.total_lines) * 100 if self.total_lines > 0 else 0
            
            # Format the log levels string
            levels_str = ', '.join(f"{level}:{count}" for level, count in stats['levels'].items())
            
            print(f"{func_name:<40} {count:<10} {bytes_size:,} bytes {percentage:>6.2f}%    {levels_str}")

def main():
    # Get log directory from environment variable with fallback
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    
    analyzer = LogAnalyzer(log_dir)
    analyzer.analyze_logs()
    analyzer.print_analysis()

if __name__ == "__main__":
    main() 