import importlib
import inspect
import pathlib

from core import Device


def load_devices() -> list:
    """Scan this directory and return one instance per Device subclass found."""
    devices = []
    here = pathlib.Path(__file__).parent
    for path in sorted(here.glob('*.py')):
        if path.stem.startswith('_'):
            continue
        mod = importlib.import_module(f'devices.{path.stem}')
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, Device) and cls is not Device and cls.name:
                devices.append(cls())
    return devices


def open_first_device():
    """Try each discovered device in order. Return the first that opens, or None."""
    for device in load_devices():
        if device.open():
            return device
    return None
