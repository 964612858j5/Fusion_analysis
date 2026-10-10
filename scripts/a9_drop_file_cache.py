"""Drop ONE file's pages from the OS page cache (posix_fadvise DONTNEED),
so the next run reads it cold -- block A9 §41. Touches nothing else.
Usage: a9_drop_file_cache.py PATH..."""
import os
import sys

for path in sys.argv[1:]:
    fd = os.open(os.path.realpath(path), os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
    print("dropped", path)
