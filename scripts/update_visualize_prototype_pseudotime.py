from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/visualize_fare_shape_autoencoder.py')
text = path.read_text()
original = text

old_parse = '''def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--clusters", nargs="+", type=int, default=[0, 2, 3, 4])
    parser.add_argument("--examples-per-cluster", type=int, default=3)
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    return parser.parse_args()
'''
new_parse = '''def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--clusters", nargs="+", type=int, default=[0, 2, 3, 4])
    parser.add_argument("--examples-per-cluster", type=int, default=3)
    parser.add_argument("--prototype-steps", type=int, default=48)
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    return parser.parse_args()
'''
if old_parse not in text:
    raise SystemExit('parse_args block not found')
text = text.replace(old_parse, new_parse, 1)

anchor = '''def configure_sequence_axis(ax, step_count: int) -> None:
    if step_count <= 0:
        return
    tick_step = 1 if step_count <= 16 else 2 if step_count <= 32 else 5
    ax.set_xlim(1, step_count)
    ax.set_xticks(np.arange(1, step_count + 1, tick_step))
    ax.grid(True, axis="x", alpha=0.18)


def plot_cluster_prototype'''
replacement = '''def configure_sequence_axis(ax, step_count: int) -> None:
    if step_count <= 0:
        return
    tick_step = 1 if step_count <= 16 else 2 if step_count <= 32 else 5
    ax.set_xlim(1, step_count)
    ax.set_xticks(np.arange(1, step_count + 1, tick_step))
    ax.grid(True, axis="x", alpha=0.18)


def resample_sequence(sequence: np.ndarray, target_steps: int) -> np.ndarray:
    source_steps = sequence.shape[0]
    if source_steps == target_steps:
        return sequence.copy()
    if source_steps == 1:
        return np.repeat(sequence, target_steps, axis=0)
    source_x = np.linspace(1.0, target_steps, num=source_steps)
    target_x = np.arange(1, target_steps + 1)
    return np.stack(
        [np.interp(target_x, source_x, sequence[:, feature_idx]) for feature_idx in range(sequence.shape[1])],
        axis=1,
    )


def plot_cluster_prototype'''
if anchor not in text:
    raise SystemExit('prototype helper anchor not found')
text = text.replace(anchor, replacement, 1)

old_proto = '''def plot_cluster_prototype(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, masks: np.ndarray, feature_names: list[str], output_path: Path) -> None:
    observed_rows = []
    observed_recons = []
    for idx in cluster_df["data_index"].tolist():
        mask = masks[idx] > 0
        observed_rows.append(originals[idx][mask])
        observed_recons.append(reconstructions[idx][mask])
    min_len = min(row.shape[0] for row in observed_rows)
    aligned_originals = np.stack([row[-min_len:] for row in observed_rows])
    aligned_recons = np.stack([row[-min_len:] for row in observed_recons])
    x = np.arange(1, min_len + 1)

    fig, axes = plt.subplots(len(feature_names), 1, figsize=(12, 3 * len(feature_names)), sharex=True)
    if len(feature_names) == 1:
        axes = [axes]
    for feature_idx, feature_name in enumerate(feature_names):
        axes[feature_idx].plot(x, aligned_originals[:, :, feature_idx].mean(axis=0), label="original", linewidth=2)
        axes[feature_idx].plot(x, aligned_recons[:, :, feature_idx].mean(axis=0), label="reconstruction", linewidth=2, linestyle="--")
        axes[feature_idx].set_title(f"cluster {cluster_id} mean {feature_name}")
        axes[feature_idx].grid(True, alpha=0.3)
        configure_sequence_axis(axes[feature_idx], min_len)
        axes[feature_idx].legend(loc="best")
    axes[-1].set_xlabel("time step t")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
'''
new_proto = '''def plot_cluster_prototype(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, masks: np.ndarray, feature_names: list[str], output_path: Path, prototype_steps: int) -> None:
    observed_rows = []
    observed_recons = []
    for idx in cluster_df["data_index"].tolist():
        mask = masks[idx] > 0
        observed_rows.append(resample_sequence(originals[idx][mask], prototype_steps))
        observed_recons.append(resample_sequence(reconstructions[idx][mask], prototype_steps))
    aligned_originals = np.stack(observed_rows)
    aligned_recons = np.stack(observed_recons)
    x = np.arange(1, prototype_steps + 1)

    fig, axes = plt.subplots(len(feature_names), 1, figsize=(12, 3 * len(feature_names)), sharex=True)
    if len(feature_names) == 1:
        axes = [axes]
    for feature_idx, feature_name in enumerate(feature_names):
        axes[feature_idx].plot(x, aligned_originals[:, :, feature_idx].mean(axis=0), label="original", linewidth=2)
        axes[feature_idx].plot(x, aligned_recons[:, :, feature_idx].mean(axis=0), label="reconstruction", linewidth=2, linestyle="--")
        axes[feature_idx].set_title(f"cluster {cluster_id} mean {feature_name}")
        axes[feature_idx].grid(True, alpha=0.3)
        configure_sequence_axis(axes[feature_idx], prototype_steps)
        axes[feature_idx].legend(loc="best")
    axes[-1].set_xlabel("pseudo-time step t")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
'''
if old_proto not in text:
    raise SystemExit('plot_cluster_prototype block not found')
text = text.replace(old_proto, new_proto, 1)

old_call = '        plot_cluster_prototype(cluster_id, cluster_df, originals, reconstructions, prepared.masks, metadata["feature_names"], prototype_path)\n'
new_call = '        plot_cluster_prototype(cluster_id, cluster_df, originals, reconstructions, prepared.masks, metadata["feature_names"], prototype_path, args.prototype_steps)\n'
if old_call not in text:
    raise SystemExit('prototype call not found')
text = text.replace(old_call, new_call, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print('updated visualizer with pseudo-time prototype')
