"""Create and check a backup of Teatro's database."""
import os
import sqlite3
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:3])
if not source.is_file():
    raise SystemExit('Source database does not exist.')
if destination.exists():
    raise SystemExit('Choose a new backup path; refusing to overwrite.')
os.umask(0o077)
with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(destination) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('Backup integrity check failed.')
print('Backup created and verified.')
