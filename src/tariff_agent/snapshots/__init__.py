"""Remembering what the tariffs were, so a change can be noticed.

This is what makes the project a monitor rather than a reader: each run stores
what it found, compares it with what was found last time, and reports only what
actually moved.
"""
