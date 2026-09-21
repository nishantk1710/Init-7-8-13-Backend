"""Turning the raw layer into something joinable.

The raw tables are a faithful copy of what SAP sent: every column, every value
as text, nothing interpreted. That is the right shape for evidence and the
wrong shape for answering questions. This package is the step between.

Three jobs, and each exists because getting it wrong fails quietly:

``matnr``   SAP's material numbers are zero-padded to 18 characters in some
            places and not in others. A join between a padded column and an
            unpadded one returns zero rows -- not an error, not a warning, an
            empty result that looks like "no matching data".

``coerce``  The raw layer is text, and some of it is text that only looks like
            a number. ``'              0.000'`` is what MARC's safety stock
            arrives as, and every arithmetic path has to strip it first.

Both are pure functions over single values, deliberately. They are the pieces
most likely to be subtly wrong, so they are the pieces that must be testable
without a database, a network, or a fixture file.
"""
