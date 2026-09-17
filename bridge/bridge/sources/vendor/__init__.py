"""Vendor SDK adapters.

These are optional. The `wpd` / `shell` sources already cover Canon, Nikon and
Sony for plain image transfer, which is what the clinical workflow needs. A
vendor SDK is only worth its licensing and deployment cost when you want the
things PTP cannot do - firing the shutter from the software, live view, or
pushing exposure settings - so each adapter here is loaded lazily and reports
itself unavailable, with a reason, when its DLL is not configured.
"""
