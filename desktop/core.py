"""Compatibility import; execution lives in the backend package."""
import sys
from chaoxing_learning_mcp import application_core as implementation
sys.modules[__name__]=implementation
