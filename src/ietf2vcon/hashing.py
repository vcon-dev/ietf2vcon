"""Content hashing in the form draft-ietf-vcon-vcon-core-02 requires.

The spec's ContentHash is the algorithm name, a hyphen, and the base64url
encoding (no padding) of the digest -- not a hex digest, and SHA-512 is the
algorithm that MUST be supported. Anything referencing an external file needs
one, so this lives in its own module rather than being reinvented per caller.
"""

import base64
import hashlib

__all__ = ["content_hash_token"]


def content_hash_token(content: bytes, algorithm: str = "sha512") -> str:
    """`sha512-<base64url digest, unpadded>` for the given bytes."""
    digest = hashlib.new(algorithm, content).digest()
    return f"{algorithm}-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
