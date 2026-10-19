"""Klaxton Desk market scanner.

Scans every Topstep-tradable futures market through the ProjectX API, ranks
them by how far they are moving away from normal behaviour, runs the Four
Gates (positioning, options surface, catalyst window) and posts the
strongest ideas to Discord.
"""
