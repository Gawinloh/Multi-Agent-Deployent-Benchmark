"""Parallel per-service multi-agent architecture (Study 5).

A manager decomposes the request once, independent per-service agents run
concurrently, and a merge step arbitrates the shared host memory budget before
validation. Exploratory third arm; the single and multi arms are unchanged.
"""

from .manager import ParallelManagerAgent, ParallelRunResult

__all__ = ["ParallelManagerAgent", "ParallelRunResult"]
