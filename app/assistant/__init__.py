"""The reservation-time assistant -- shared between Initiative 08 and 13.

Deliberately not inside either initiative package. The BAdI pop-up knows only a
material and a plant; it cannot know whether that material is 80-series or OAR,
so the entry point cannot live under one initiative's prefix and the session it
mints cannot belong to one initiative's namespace. See ``router.py`` for the
decision and ``models.py`` for what that forces on the schema.
"""
