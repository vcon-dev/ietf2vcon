"""IETF Meeting to vCon Converter.

Convert IETF meeting sessions into vCon (Virtual Conversation Container) format,
including video recordings, meeting materials, transcriptions, and chat logs.
"""

__version__ = "0.2.0"

from .converter import IETFSessionConverter
from .vcon_builder import VConBuilder

__all__ = ["IETFSessionConverter", "VConBuilder"]
