"""Bloodbank-owned Hermes gateway plugin."""

from .adapter import BloodbankAdapter, register

__all__ = ["BloodbankAdapter", "register"]
