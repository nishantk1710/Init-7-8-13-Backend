"""SAP integration adapter (OData and friends).

``SapGateway`` (gateway.py) is the shared entry point -- one client for every
initiative, not one per initiative. It fans out to ``LiveSapGateway`` for
entity sets that are live and usable in this tenant, and
``ReducedMockSapGateway`` for the handful that currently are not.
"""

from app.integrations.sap.gateway import SapGateway, get_sap_gateway

__all__ = ["SapGateway", "get_sap_gateway"]
