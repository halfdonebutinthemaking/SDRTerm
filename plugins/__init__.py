import importlib
import inspect
import pathlib

from core import Decoder


def load_plugins() -> dict:
    """Scan this directory and return {name: instance} for every Decoder subclass found."""
    plugins = {}
    here = pathlib.Path(__file__).parent
    for path in sorted(here.glob('*.py')):
        if path.stem.startswith('_'):
            continue
        mod = importlib.import_module(f'plugins.{path.stem}')
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, Decoder) and cls is not Decoder and cls.name:
                plugins[cls.name] = cls()
    return plugins
