import importlib
import inspect
import pathlib

from core import Decoder


def load_plugins() -> dict:
    """Scan this directory and return {name: instance} for every Decoder subclass found.

    Discovers plugins in two layouts:
      - flat:   plugins/<name>.py          (legacy, still supported)
      - subdir: plugins/<name>/<name>.py   (preferred for new plugins)
    """
    plugins = {}
    here = pathlib.Path(__file__).parent

    # Flat layout (kept for backwards compatibility)
    for path in sorted(here.glob('*.py')):
        if path.stem.startswith('_'):
            continue
        mod = importlib.import_module(f'plugins.{path.stem}')
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, Decoder) and cls is not Decoder and cls.name:
                plugins[cls.name] = cls()

    # Subdirectory layout: plugins/<name>/<name>.py
    for subdir in sorted(p for p in here.iterdir() if p.is_dir() and not p.name.startswith('_')):
        py_file = subdir / f'{subdir.name}.py'
        if not py_file.exists():
            continue
        mod = importlib.import_module(f'plugins.{subdir.name}.{subdir.name}')
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, Decoder) and cls is not Decoder and cls.name:
                if cls.name not in plugins:   # flat layout wins on name collision
                    plugins[cls.name] = cls()

    return plugins
