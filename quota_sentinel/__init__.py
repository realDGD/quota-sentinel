"""Quota Sentinel — macOS-first local AI quota scheduler and Feishu notifier.

This package is the Python side of a strangler migration: scheduler state
access first (quota_sentinel.state), then further subsystems, each proven
against the existing shell regression suites before the next layer moves.
See ARCHITECTURE.md for the ownership boundaries this package must respect.
"""
