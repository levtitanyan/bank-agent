"""Turning retrieved passages into verified, normalized tariff values.

The model's only job here is reading: which passage answers a field, and what
text to quote. Everything that decides whether the answer is *usable* - that the
quote really occurs in the cited chunk, that the number is well formed, that the
effective rate is not below the nominal one - is deterministic Python.
"""
