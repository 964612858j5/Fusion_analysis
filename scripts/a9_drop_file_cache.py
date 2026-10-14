"""Drop files' pages from the OS page cache (posix_fadvise DONTNEED), so the
next run reads them cold -- block A9 §41. Touches nothing else.
Usage: a9_drop_file_cache.py PATH...

R6 §12.3: a PATH that is a directory (a Zarr store) is resolved and walked
recursively -- every chunk and metadata file. Prints one line per file
argument, one summary line per directory (files done, files failed,
directories that could not be listed), and exits 1 when any of them failed.
DONTNEED asks the kernel to evict; it does not prove the cache is cold
(pages in use elsewhere may stay).
"""
import os
import sys


def drop(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


failed = 0
for arg in sys.argv[1:]:
    real = os.path.realpath(arg)
    if not os.path.isdir(real):
        drop(real)
        print("dropped", arg)
        continue
    done = bad = 0
    walk_errors = []
    for root, _dirs, files in os.walk(real, followlinks=True, onerror=walk_errors.append):
        for name in files:
            try:
                drop(os.path.join(root, name))
                done += 1
            except OSError:
                bad += 1
    failed += bad + len(walk_errors)
    print(f"dropped dir {arg} -> {real}: files {done} failed {bad} unreadable dirs {len(walk_errors)}")
sys.exit(1 if failed else 0)
