import sys
import os
from cx_Freeze import setup, Executable

# Read version from version.txt
with open('version.txt', 'r') as f:
    version = f.read().strip()

# Build options
build_exe_options = {
    "packages": [
        "os", "flask", "sqlalchemy", "requests", "aiohttp", "bs4",
        "grpc", "guessit", "urwid", "plexapi", "PIL", "supervisor",
        "psutil", "api_tracker"
    ],
    "includes": [
        "extensions",
        "cli_battery",
        "logging_config",  # Add this line
        # Add any other modules that might be imported dynamically
    ],
    "excludes": ["tkinter"],
    "include_files": [
        ('main.py', 'main.py'),
        ('cli_battery', 'cli_battery'),
        ('version.txt', 'version.txt'),
        ('extensions.py', 'extensions.py'),
        ('logging_config.py', 'logging_config.py'),  # Add this line
        # Include any other necessary files or directories
    ],
    "include_msvcr": True,
}

base = None
if sys.platform == "win32":
    base = "Win32GUI"

executables = [
    Executable(
        "windows_wrapper.py",
        base=base,
        target_name="cli_debrid.exe",
        icon="static/favicon.ico"
    )
]

setup(
    name="cli_debrid",
    version=version,
    description="CLI Debrid Application",
    options={"build_exe": build_exe_options},
    executables=executables
)