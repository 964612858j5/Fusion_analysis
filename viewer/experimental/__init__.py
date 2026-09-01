"""Experimental viewer components — NOT part of the production path.

Nothing in `viewer/` (outside this package), `ui/` or `main.py` may import
from here; a test enforces that boundary with an AST scan. Code lives here
when it is worth keeping and measuring but is not wired into the shipped
Explore stack: a benchmark or the demo opts in explicitly by importing and
instantiating it.
"""
