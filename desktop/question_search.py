"""Compatibility import for the backend search implementation."""
import sys
from chaoxing_learning_mcp import question_search as implementation
sys.modules[__name__]=implementation
