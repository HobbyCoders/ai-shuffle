"""Android Virtual Device definitions, written directly (no avdmanager).

An AVD is two files: ``<home>/<id>.ini`` points at ``<home>/<id>.avd/``, and
``config.ini`` inside it holds the hardware profile. The emulator creates the
device's disks from the system image on its first boot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from app.core.android_emulator import sdk_repo
from app.core.android_emulator.layout import SdkLayout


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    name: str
    width: int
    height: int
    density: int
    ram_mb: int = 2048
    cores: int = 2
    data_partition: str = "6G"


#: The devices this worker offers. "pixel" is a Pixel-sized phone (1080x2400 at
#: 420 dpi, 411x914 dp), created on its first start.
PROFILES: Dict[str, DeviceProfile] = {
    "pixel": DeviceProfile(id="pixel", name="Pixel", width=1080, height=2400, density=420),
}


def config_values(profile: DeviceProfile, system_image: str) -> Dict[str, str]:
    api, tag, abi = sdk_repo.system_image_parts(system_image)
    cpu = {"x86_64": "x86_64", "x86": "x86", "arm64-v8a": "arm64"}[abi]
    return {
        "AvdId": profile.id,
        "avd.ini.displayname": profile.name,
        "avd.ini.encoding": "UTF-8",
        "PlayStore.enabled": "false",
        "abi.type": abi,
        "hw.cpu.arch": cpu,
        "hw.cpu.ncore": str(profile.cores),
        "hw.ramSize": f"{profile.ram_mb}M",
        "vm.heapSize": "256M",
        "disk.dataPartition.size": profile.data_partition,
        "image.sysdir.1": f"system-images/android-{api}/{tag}/{abi}/",
        "tag.id": tag,
        "tag.display": "Google APIs" if tag == "google_apis" else tag,
        "target": f"android-{api}",
        "hw.device.manufacturer": "Generic",
        "hw.device.name": profile.id,
        "hw.lcd.width": str(profile.width),
        "hw.lcd.height": str(profile.height),
        "hw.lcd.density": str(profile.density),
        "hw.initialOrientation": "portrait",
        "skin.dynamic": "yes",
        "skin.name": f"{profile.width}x{profile.height}",
        "showDeviceFrame": "no",
        # Software rendering: works with no display and no host GPU.
        "hw.gpu.enabled": "yes",
        "hw.gpu.mode": "swiftshader_indirect",
        "hw.keyboard": "yes",
        "hw.mainKeys": "no",
        "hw.dPad": "no",
        "hw.trackBall": "no",
        "hw.audioInput": "no",
        "hw.audioOutput": "no",
        "hw.camera.back": "none",
        "hw.camera.front": "none",
        "hw.sdCard": "no",
        "hw.gps": "yes",
        "hw.battery": "yes",
        "hw.accelerometer": "yes",
        "hw.sensors.orientation": "yes",
        "hw.sensors.proximity": "yes",
        "fastboot.forceColdBoot": "yes",
        "fastboot.forceFastBoot": "no",
        "runtime.network.latency": "none",
        "runtime.network.speed": "full",
    }


def _ini(values: Dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def avd_dir(layout: SdkLayout, profile: DeviceProfile) -> Path:
    return layout.avd_home / f"{profile.id}.avd"


def write_avd(layout: SdkLayout, profile: DeviceProfile, system_image: str) -> Path:
    """Create or refresh the device's definition. Its disks are left alone."""
    api, _tag, _abi = sdk_repo.system_image_parts(system_image)
    folder = avd_dir(layout, profile)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.ini").write_text(_ini(config_values(profile, system_image)), encoding="utf-8")
    (layout.avd_home / f"{profile.id}.ini").write_text(_ini({
        "avd.ini.encoding": "UTF-8",
        "path": str(folder),
        "path.rel": f"avd/{profile.id}.avd",
        "target": f"android-{api}",
    }), encoding="utf-8")
    return folder


def clear_locks(layout: SdkLayout, profile: DeviceProfile) -> int:
    """Remove the lock files a killed emulator leaves behind. Returns how many."""
    folder = avd_dir(layout, profile)
    removed = 0
    if not folder.is_dir():
        return 0
    for lock in folder.glob("*.lock"):
        try:
            if lock.is_dir():
                for child in lock.iterdir():
                    child.unlink()
                lock.rmdir()
            else:
                lock.unlink()
            removed += 1
        except OSError:
            pass
    return removed
