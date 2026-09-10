"""Adapters to external platforms.

One shared adapter per platform, used by initiative services through
``app.shared`` -- external client code must not be scattered across the
initiative packages. Structure only for now; nothing is implemented.

    initiative service -> shared domain/service -> integration adapter -> external platform
"""
