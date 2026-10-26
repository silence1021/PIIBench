"""Capture, syntheticize, and validate reproducible web-page snapshots."""

from .manifest import FieldBinding, PageSpec, PrivacyOptions, SnapshotManifest, load_manifest

__all__ = ["FieldBinding", "PageSpec", "PrivacyOptions", "SnapshotManifest", "load_manifest"]
