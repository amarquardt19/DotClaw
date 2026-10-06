from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/visualize_fare_shape_autoencoder.py')
text = path.read_text()
original = text

helper_anchor = '''def make_time_axis(mask: np.ndarray, seq_len: int) -> np.ndarray:
    observed_idx = np.flatnonzero(mask > 0)
    if observed_idx.size == 0:
        return np.arange(seq_len)
    start = int(observed_idx[0])
    end = int(observed_idx[-1]) + 1
    return np.arange(start, end)
'''
helper_replacement = '''def make_time_axis(mask: np.ndarray, seq_len: int) -> np.ndarray:
    observed_idx = np.flatnonzero(mask > 0)
    if observed_idx.size == 0:
        return np.arange(seq_len)
    start = int(observed_idx[0])
    end = int(observed_idx[-1]) + 1
    return np.arange(start, end)


def configure_sequence_axis(ax, step_count: int) -> None:
    if step_count <= 0:
        return
    tick_step = 1 if step_count <= 16 else 2 if step_count <= 32 else 5
    ax.set_xlim(1, step_count)
    ax.set_xticks(np.arange(1, step_count + 1, tick_step))
    ax.grid(True, axis="x", alpha=0.18)
'''
if helper_anchor not in text:
    raise SystemExit('make_time_axis anchor not found')
text = text.replace(helper_anchor, helper_replacement, 1)

replacements = [
    ('    x = np.arange(min_len)\n', '    x = np.arange(1, min_len + 1)\n'),
    ('        axes[feature_idx].grid(True, alpha=0.3)\n        axes[feature_idx].legend(loc="best")\n', '        axes[feature_idx].grid(True, alpha=0.3)\n        configure_sequence_axis(axes[feature_idx], min_len)\n        axes[feature_idx].legend(loc="best")\n'),
    ('    axes[-1].set_xlabel("observed sequence step")\n', '    axes[-1].set_xlabel("time step t")\n'),
    ('        recon = reconstructions[data_index][x]\n', '        recon = reconstructions[data_index][x]\n        x = np.arange(1, orig.shape[0] + 1)\n'),
    ('            ax.grid(True, alpha=0.25)\n', '            ax.grid(True, alpha=0.25)\n            configure_sequence_axis(ax, orig.shape[0])\n'),
]

for old, new in replacements:
    if old not in text:
        raise SystemExit(f'replacement anchor not found: {old!r}')
    text = text.replace(old, new, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print('updated visualizer t-axis')
