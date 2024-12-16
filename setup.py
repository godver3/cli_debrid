import sys
import os
from cx_Freeze import setup, Executable

# Read version from version.txt
with open('version.txt', 'r') as f:
    version = f.read().strip()

# Get the base directory
base_dir = os.path.abspath(os.path.dirname(__file__))

# Function to create data_files list with proper paths
def get_data_files(directory):
    data_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            source = os.path.join(root, file)
            # Get the relative path from the base directory
            rel_path = os.path.relpath(source, base_dir)
            # Get the target directory
            target_dir = os.path.dirname(rel_path)
            data_files.append((target_dir, [source]))
    return data_files

# Collect all data files
data_dirs = [
    'templates', 'cli_battery', 'database', 'content_checkers',
    'debrid', 'metadata', 'queues', 'routes', 'scraper',
    'static', 'utilities'
]

data_files = []
for directory in data_dirs:
    data_files.extend(get_data_files(directory))

# Add individual files
additional_files = [
    'version.txt',
    'tooltip_schema.json',
    os.path.join('static', 'favicon.png'),
]

for file in additional_files:
    if os.path.exists(file):
        target_dir = os.path.dirname(file) if os.path.dirname(file) else '.'
        data_files.append((target_dir, [file]))

# Build options
build_exe_options = {
    "packages": [
        "os", "flask", "sqlalchemy", "requests", "aiohttp", "bs4",
        "grpc", "guessit", "urwid", "plexapi", "PIL", "supervisor",
        "psutil", "api_tracker", "multiprocessing", "bencodepy", "tenacity",
        "appdirs", "pytrakt", "tzlocal"
    ],
    "includes": [
        "database",
        "database.core",
        "database.collected_items",
        "database.blacklist",
        "database.schema_management",
        "database.poster_management",
        "database.statistics",
        "database.wanted_items",
        "database.database_reading",
        "database.database_writing",
        "content_checkers.trakt",
        "logging_config",
        "main",
        "metadata.Metadata",
    ],
    "include_files": data_files,
    "excludes": ["tkinter"],
}

# Executable configuration
target = Executable(
    script="windows_wrapper.py",
    target_name="cli_debrid.exe",
    base=None,  # "Win32GUI" if sys.platform == "win32" else None,
    icon="static/favicon.ico" if os.path.exists("static/favicon.ico") else None,
)

setup(
    name="cli_debrid",
    version=version,
    description="CLI Debrid Application",
    options={"build_exe": build_exe_options},
    executables=[target]
)