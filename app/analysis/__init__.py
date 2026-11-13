"""Phase B analysis roles: source verification, product map, evidence, language.

All four roles are deterministic in mock mode and keep every extracted claim
attached to a registered source code. Nothing here invents facts: what cannot be
derived from the corpus becomes an explicit limitation.
"""

from app.analysis.common import propositions_by_source, registry_map

__all__ = ["propositions_by_source", "registry_map"]
