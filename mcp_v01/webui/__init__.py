"""Lightweight local Web UI over the phase-one materials library.

Read paths reuse jty_materials.query / store. All writes go through the same
serialized manager RPC used by the offline CLI; the web layer adds no new
write semantics of its own.
"""

# WebUI release version (independent from the jty-mcp storage schema).
WEBUI_VERSION = "0.5.0"
