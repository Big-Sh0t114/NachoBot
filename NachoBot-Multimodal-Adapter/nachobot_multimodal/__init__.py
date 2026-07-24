"""Stable public import namespace for NachoBot's multimodal adapter.

Implementation modules remain in the conventional ``src/`` layout while this
package prevents collisions with the NachoBot core's own ``src`` package.
"""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent / "src")]
