"""Golden J11 frames.

Unless noted otherwise, every constant is a frame sample copied from ROHM's
public B-route application note (No. 63AN028E, §5.1 "Initial setting", §5.2
"Active scan", §5.3 "B-route Connection", §5.4 "Data transmission"). The sample
numbers (F1, F3, ...) are the note's own row labels.

See README.md in this directory for the fixture policy.
"""

from __future__ import annotations


def unhex(text: str) -> bytes:
    """Decode a whitespace-formatted hex dump."""
    return bytes.fromhex(text.replace(" ", "").replace("\n", ""))


# F1: Reset Hardware Request. No data block.
RESET_REQUEST = unhex("D0 EA 83 FC 00 D9 00 04 04 16 00 00")

# F2: Notify Startup Completion. No data block.
STARTUP_NOTIFICATION = unhex("D0 F9 EE 5D 60 19 00 04 03 91 00 00")

# F3: Set Initial Settings Request. Dual mode, sleep disabled, channel 4, 20 mW.
INITIAL_SETTINGS_REQUEST_CHANNEL_4 = unhex(
    "D0 EA 83 FC 00 5F 00 08 03 A0 00 09 05 00 04 00"
)

# F4: Set Initial Settings Response. Response result 0x01 (succeeded).
INITIAL_SETTINGS_RESPONSE = unhex("D0 F9 EE 5D 20 5F 00 05 03 98 00 01 01")

# F5: Set Route-B PANA Authentication Information Request. The 32-character
# authentication ID and 12-character password are the note's placeholders.
CREDENTIALS_REQUEST = unhex(
    "D0 EA 83 FC 00 54 00 30 03 BD 09 D4"
    "30 30 31 31 32 32 33 33 34 34 35 35 36 36 37 37"
    "38 38 39 39 41 41 42 42 43 43 44 44 45 45 46 46"
    "30 31 32 33 34 35 36 37 38 39 41 42"
)
DOCUMENTED_AUTH_ID = "00112233445566778899AABBCCDDEEFF"
DOCUMENTED_PASSWORD = "0123456789AB"

# F6: Set Route-B PANA Authentication Information Response.
CREDENTIALS_RESPONSE = unhex("D0 F9 EE 5D 20 54 00 05 03 8D 00 01 01")

# F7: Execute Active Scan Request. Scan time 6, channel mask 0x0003FFF0,
# pairing ID set to the last eight characters of the authentication ID.
ACTIVE_SCAN_REQUEST = unhex(
    "D0 EA 83 FC 00 51 00 12 03 9C 04 1D 06 00 03 FF F0 01 43 43 44 44 45 45 46 46"
)

# F8: Execute Active Scan scan result Notification, no beacon response,
# channel 4.
ACTIVE_SCAN_NOTIFICATION_EMPTY = unhex("D0 F9 EE 5D 40 51 00 06 03 AB 00 05 01 04")

# F8: Execute Active Scan scan result Notification, one beacon response on
# channel 12, PAN ID 0xBCDE, RSSI 0xDE.
ACTIVE_SCAN_NOTIFICATION_RESPONSE = unhex(
    "D0 F9 EE 5D 40 51 00 12 03 B7 06 BA 00 0C 01 00 50 C2 FF FE DC 28 22 BC DE DE"
)

# F9: Execute Active Scan Response.
ACTIVE_SCAN_RESPONSE = unhex("D0 F9 EE 5D 20 51 00 05 03 8A 00 01 01")

# F10: Set Initial Settings Request built from the scan result (channel 12).
INITIAL_SETTINGS_REQUEST_CHANNEL_12 = unhex(
    "D0 EA 83 FC 00 5F 00 08 03 A0 00 11 05 00 0C 00"
)

# F12: Initiate Route-B Operation Request.
ROUTE_B_START_REQUEST = unhex("D0 EA 83 FC 00 53 00 04 03 90 00 00")

# F13: Initiate Route-B Operation Response, connected on channel 12.
ROUTE_B_START_RESPONSE = unhex(
    "D0 F9 EE 5D 20 53 00 11 03 98 06 BA 01 0C BC DE 00 50 C2 FF FE DC 28 22 DE"
)

# F14: Open UDP Port Request for the ECHONET Lite port 3610 (0x0E1A).
UDP_OPEN_REQUEST = unhex("D0 EA 83 FC 00 05 00 06 03 44 00 28 0E 1A")

# F15: Open UDP Port Response.
UDP_OPEN_RESPONSE = unhex("D0 F9 EE 5D 20 05 00 05 03 3E 00 01 01")

# F16: Initiate Route-B PANA Request.
PANA_START_REQUEST = unhex("D0 EA 83 FC 00 56 00 04 03 93 00 00")

# F17: Initiate Route-B PANA Response.
PANA_START_RESPONSE = unhex("D0 F9 EE 5D 20 56 00 05 03 8F 00 01 01")

# F18: Notify PANA Authentication Result, authentication succeeded.
PANA_RESULT_NOTIFICATION = unhex(
    "D0 F9 EE 5D 60 28 00 0D 03 A9 04 36 01 00 50 C2 FF FE DC 28 22"
)

# F20: Transmit Data Request carrying an ECHONET Lite Get for the scheduled
# cumulative energy properties, forward (0xEA) and reverse (0xEB).
TRANSMIT_DATA_REQUEST = unhex(
    "D0 EA 83 FC 00 08 00 2A 03 6B 0A 75"
    "FE 80 00 00 00 00 00 00 02 50 C2 FF FE DC 28 22"
    "0E 1A 0E 1A 00 10"
    "10 81 00 06 05 FF 01 02 88 01 62 02 EA 00 EB 00"
)

# F21: Transmit Data Response. Result 0x00 (queued nothing, transmission
# succeeded) followed by the first five payload bytes.
TRANSMIT_DATA_RESPONSE = unhex(
    "D0 F9 EE 5D 20 08 00 0B 03 47 00 9D 01 00 10 81 00 06 05"
)

# F22: Notify Data Reception carrying the ECHONET Lite Get_Res for the two
# scheduled cumulative-energy properties requested by F20.
DATA_RECEPTION_NOTIFICATION = unhex(
    "D0 F9 EE 5D 60 18 00 45 03 D1 10 8D"
    "FE 80 00 00 00 00 00 00 02 50 C2 FF FE DC 28 22"
    "0E 1A 0E 1A 22 A9 00 02 CB 00 26"
    "10 81 00 06 02 88 01 05 FF 01 72 02"
    "EA 0B 07 E2 0A 02 0E 1E 00 00 03 73"
    "AF"
    "EB 0B 07 E2 0A 02 0E 1E 00 00 01 6A"
    "72"
)

# F19: Notify Data Reception carrying the meter's spontaneous instance-list
# notification, received right after PANA authentication.
INSTANCE_LIST_NOTIFICATION = unhex(
    "D0 F9 EE 5D 60 18 00 38 03 C4 0E 1C"
    "FE 80 00 00 00 00 00 00 02 50 C2 FF FE DC 28 22"
    "0E 1A 0E 1A BC DE 00 02 DE 00 19"
    "10 81 00 01 02 88 01 05 FF 01 73 01"
    "EA 0B 07 DF 08 1F 15 1E 00 00 00 0D"
    "AC"
)
