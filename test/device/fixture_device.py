"""A stored device as a sign-in leaves it, on an injected hardware ID; tests never read the real one."""

from unittest.mock import patch

from openmachine_device.store import Identity

HARDWARE = "hardware-A"


def stored_device(test, store, device: dict) -> tuple[Identity, dict]:
    """Writes a new device key, injects this computer's hardware ID for the test, and returns the key with the
    config fields that store device bound to both."""
    for target in ("openmachine_device.store.hardware_id", "openmachine_device.companion.hardware_id",
                   "openmachine_device.cloud.hardware_id", "openmachine_device.cli.hardware_id"):
        hardware = patch(target, return_value=HARDWARE)
        hardware.start()
        test.addCleanup(hardware.stop)
    key = Identity.create(store)
    device = {**device, "key_fingerprint": key.fingerprint, "hardware_id": HARDWARE}
    return key, {"device": device, "devices": [device]}
