"""Site data helpers shared across dashboard modules.

Re-exports from ops_dashboard.sections.sites so that section modules
can import without creating cross-section dependencies.
"""
from ops_dashboard.sections.sites import canonical_name, load_sites, site_id_from_doc

__all__ = ["canonical_name", "load_sites", "site_id_from_doc"]
