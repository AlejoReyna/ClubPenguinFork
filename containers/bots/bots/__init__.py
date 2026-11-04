"""Service of AI-controlled penguins (NPC) for the Club Penguin AS2 lab.

The service keeps normal client sessions against the Houdini login/world
servers, makes the penguins walk around a configured room using local logic
only, and generates short chat replies with Moonshot/Kimi when a human player
mentions a bot. Sessions, movement, provider access, memory and metrics are
independent modules; the model is never given tools or privileges.
"""

__version__ = '0.1.0'
