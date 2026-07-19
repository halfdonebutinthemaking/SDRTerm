import datetime
import glob
import json
import os

from core import AppState, _nearest_bw, _required_bw

_PRESET_FIELDS = (
    'center_hz', 'bw_hz', 'gain_db', 'gain_auto', 'iq_corr',
    'fm_bw_hz', 'nrsc5_sc_outer', 'waterfall_active', 'active_decoders',
)


_PRESET_DIR = 'presets'


def _preset_default_name() -> str:
    os.makedirs(_PRESET_DIR, exist_ok=True)
    return os.path.join(_PRESET_DIR, 'preset_{}.sdrterm'.format(
        datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')))


def _migrate_preset(data: dict) -> dict:
    """Upgrade preset data from older format versions in-place."""
    version = data.get('version', 0)
    if version == 0:
        # v0→v1: scan_freq_min/max move from top-level to plugin_states['range-scan']
        rs = {}
        for key in ('scan_freq_min', 'scan_freq_max'):
            if key in data:
                rs[key] = data.pop(key)
        if rs:
            data.setdefault('plugin_states', {}).setdefault('range-scan', {}).update(rs)
        data['version'] = 1
    return data


def _save_preset_to(path: str, state: AppState, all_plugins: list) -> None:
    data = {'version': 1}
    for f in _PRESET_FIELDS:
        v = getattr(state, f)
        data[f] = list(v) if isinstance(v, set) else v
    data['plugin_order']  = [p.name for p in all_plugins]
    data['plugin_states'] = {p.name: s
                              for p in all_plugins
                              if (s := p.save_state())}
    with open(path, 'w') as fh:
        json.dump(data, fh, indent=2)


def _load_preset(path: str, state: AppState, plugins: list = None) -> bool:
    try:
        with open(path) as fh:
            data = json.load(fh)
        data = _migrate_preset(data)
        for f in _PRESET_FIELDS:
            if f not in data:
                continue
            v = data[f]
            if f == 'active_decoders':
                v = set(v) | {'spectrum'}
            setattr(state, f, v)
        if 'plugin_order' in data:
            state.plugin_order = data['plugin_order']
        if plugins and 'plugin_states' in data:
            ps = data['plugin_states']
            for p in plugins:
                if p.name in ps:
                    p.load_state(ps[p.name])
        return True
    except Exception:
        return False


def _find_presets() -> list:
    return sorted(glob.glob(os.path.join(_PRESET_DIR, '*.sdrterm')))


def _apply_preset(path: str, state: AppState, registry: dict, sdr,
                  all_plugins: list = None) -> bool:
    """Load a preset at runtime: start/stop decoders and apply hardware settings."""
    old_active = set(state.active_decoders)
    if not _load_preset(path, state, all_plugins):
        return False
    if all_plugins is not None and state.plugin_order:
        _om = {name: i for i, name in enumerate(state.plugin_order)}
        all_plugins.sort(key=lambda p: _om.get(p.name, len(state.plugin_order)))
    new_active = state.active_decoders
    for name in old_active - new_active:
        if name in registry:
            registry[name].stop()
    needed = _required_bw(new_active, registry)
    clamped = _nearest_bw(needed, sdr.supported_bandwidths)
    if state.bw_hz not in sdr.supported_bandwidths:
        state.bw_hz = min(sdr.supported_bandwidths,
                          key=lambda b: abs(b - state.bw_hz))
    if state.bw_hz < clamped:
        state.bw_hz = clamped
    sdr.sample_rate = state.bw_hz
    sdr.center_freq = state.center_hz
    sdr.gain        = 'auto' if state.gain_auto else state.gain_db
    for name in new_active - old_active:
        if name in registry:
            registry[name].start(state)
    return True
