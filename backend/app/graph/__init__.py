"""Supply-chain network graph: typed relationships over the canonical registry.

The graph is a *view and rule layer* over existing tables - ``supply_relationships`` and ``product_links`` (real
foreign keys to the registry) plus the facility owner link - not a second copy of suppliers, facilities or products.
"""
