"""Configuration names only; no real channel or credentials in the repository."""
import os

# Synthetic fixture identifier. Set the real approved channel in the owner process.
CHANNEL = os.environ.get('MM_CHANNEL_ID', 'a' * 26)
