from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/visualize_fare_shape_autoencoder.py')
text = path.read_text()
original = text

text = text.replace('import matplotlib.pyplot as plt\n', 'import matplotlib.pyplot as plt\nimport matplotlib.ticker as mticker\n')

old_block = '''def make_time_axis(mask: np.ndarray, seq_len: int) -> np.ndarray:
    observed_idx = np.flatnonzero(mask > 0)
    if observed_idx.size == 0:
        return np.arange(seq_len)
    start = int(observed_idx[0])
    end = int(observed_idx[-1]) + 1
    return np.arange(start, end)


def plot_cluster_prototype(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, masks: np.ndarray, feature_names: list[str], output_path: Path) -> None:
'''
new_block = '''def load_day_axes(metadata: dict, cli_args: argparse.Namespace) -> dict[str, np.ndarray]:
    df = pd.read_excel(metadata["workbook"], sheet_name=metadata["sheet_name"])
    if "matched_target_flight" in df.columns:
        df = df[df["matched_target_flight"].fillna(False).astype(bool)].copy()
    df["query_timestamp"] = pd.to_datetime(df.get("query_timestamp"), errors="coerce")
    df["departure_date"] = pd.to_datetime(df.get("departure_date"), errors="coerce")
    min_days_until_departure = metadata.get("min_days_until_departure", 1)
    df = df[df["days_until_departure"].notna()].copy()
    df = df[pd.to_numeric(df["days_until_departure"], errors="coerce") >= min_days_until_departure].copy()
    df = df[pd.to_numeric(df["days_until_departure"], errors="coerce") <= cli_args.horizon_days].copy()
    df = df[df["route_service_label"].notna() & df["departure_date"].notna()].copy()

    day_axes: dict[str, np.ndarray] = {}
    for service_label, group in df.groupby("route_service_label", dropna=True):
        group = training.add_sequence_features(group, cli_args.horizon_days)
        if len(group) < cli_args.min_observations:
            continue
        days = pd.to_numeric(group["days_until_departure"], errors="coerce").to_numpy(dtype=np.float32)
        if days.shape[0] > cli_args.max_seq_len:
            days = days[-cli_args.max_seq_len :]
        day_axes[str(service_label)] = days
    return day_axes


def configure_day_axis(ax, x_values: np.ndarray) -> None:
    if x_values.size == 0:
        return
    ax.set_xlim(float(np.nanmax(x_values)), float(np.nanmin(x_values)))
    ax.xaxis.set_minor_locator(mticker.MultipleLocator(1))
    major_step = 7 if (float(np.nanmax(x_values)) - float(np.nanmin(x_values))) > 14 else 1
    ax.xaxis.set_major_locator(mticker.MultipleLocator(major_step))
    ax.grid(True, which="major", alpha=0.3)
    ax.grid(True, which="minor", alpha=0.12)


def plot_cluster_prototype(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, feature_names: list[str], day_axes: dict[str, np.ndarray], output_path: Path) -> None:
'''
if old_block not in text:
    raise SystemExit('old time-axis block not found')
text = text.replace(old_block, new_block, 1)

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
    x = np.arange(min_len)

    fig, axes = plt.subplots(len(feature_names), 1, figsize=(12, 3 * len(feature_names)), sharex=True)
    if len(feature_names) == 1:
        axes = [axes]
    for feature_idx, feature_name in enumerate(feature_names):
        axes[feature_idx].plot(x, aligned_originals[:, :, feature_idx].mean(axis=0), label="original", linewidth=2)
        axes[feature_idx].plot(x, aligned_recons[:, :, feature_idx].mean(axis=0), label="reconstruction", linewidth=2, linestyle="--")
        axes[feature_idx].set_title(f"cluster {cluster_id} mean {feature_name}")
        axes[feature_idx].grid(True, alpha=0.3)
        axes[feature_idx].legend(loc="best")
    axes[-1].set_xlabel("observed sequence step")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
'''
new_proto = '''def plot_cluster_prototype(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, feature_names: list[str], day_axes: dict[str, np.ndarray], output_path: Path) -> None:
    observed_rows = []
    observed_recons = []
    observed_days = []
    for _, row in cluster_df.iterrows():
        idx = int(row["data_index"])
        sample_key = str(row["sample_key"])
        day_axis = day_axes.get(sample_key)
        if day_axis is None or day_axis.size == 0:
            continue
        observed_rows.append(originals[idx][-day_axis.shape[0] :])
        observed_recons.append(reconstructions[idx][-day_axis.shape[0] :])
        observed_days.append(day_axis)
    min_len = min(row.shape[0] for row in observed_rows)
    aligned_originals = np.stack([row[-min_len:] for row in observed_rows])
    aligned_recons = np.stack([row[-min_len:] for row in observed_recons])
    aligned_days = np.stack([days[-min_len:] for days in observed_days])
    x = aligned_days.mean(axis=0)

    fig, axes = plt.subplots(len(feature_names), 1, figsize=(12, 3 * len(feature_names)), sharex=True)
    if len(feature_names) == 1:
        axes = [axes]
    for feature_idx, feature_name in enumerate(feature_names):
        axes[feature_idx].plot(x, aligned_originals[:, :, feature_idx].mean(axis=0), label="original", linewidth=2)
        axes[feature_idx].plot(x, aligned_recons[:, :, feature_idx].mean(axis=0), label="reconstruction", linewidth=2, linestyle="--")
        axes[feature_idx].set_title(f"cluster {cluster_id} mean {feature_name}")
        configure_day_axis(axes[feature_idx], x)
        axes[feature_idx].legend(loc="best")
    axes[-1].set_xlabel("days until departure")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
'''
if old_proto not in text:
    raise SystemExit('old prototype function not found')
text = text.replace(old_proto, new_proto, 1)

old_examples = '''def plot_cluster_examples(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, masks: np.ndarray, feature_names: list[str], output_path: Path) -> None:
    fig, axes = plt.subplots(len(cluster_df), len(feature_names), figsize=(4 * len(feature_names), 2.8 * len(cluster_df)), sharex=False)
    if len(cluster_df) == 1:
        axes = np.array([axes])
    if len(feature_names) == 1:
        axes = axes.reshape(len(cluster_df), 1)

    for row_idx, (_, row) in enumerate(cluster_df.iterrows()):
        data_index = int(row["data_index"])
        x = make_time_axis(masks[data_index], masks.shape[1])
        orig = originals[data_index][x]
        recon = reconstructions[data_index][x]
        for feature_idx, feature_name in enumerate(feature_names):
            ax = axes[row_idx, feature_idx]
            ax.plot(x, orig[:, feature_idx], linewidth=1.8, label="original")
            ax.plot(x, recon[:, feature_idx], linewidth=1.6, linestyle="--", label="reconstruction")
            if row_idx == 0:
                ax.set_title(feature_name)
            if feature_idx == 0:
                ax.set_ylabel(f"cluster {cluster_id}\n{row['sample_key']}\nloss={row['reconstruction_loss']:.3f}")
            ax.grid(True, alpha=0.25)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right")
    fig.tight_layout(rect=[0, 0, 0.98, 0.98])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
'''
new_examples = '''def plot_cluster_examples(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, feature_names: list[str], day_axes: dict[str, np.ndarray], output_path: Path) -> None:
    fig, axes = plt.subplots(len(cluster_df), len(feature_names), figsize=(4 * len(feature_names), 2.8 * len(cluster_df)), sharex=False)
    if len(cluster_df) == 1:
        axes = np.array([axes])
    if len(feature_names) == 1:
        axes = axes.reshape(len(cluster_df), 1)

    for row_idx, (_, row) in enumerate(cluster_df.iterrows()):
        data_index = int(row["data_index"])
        x = day_axes.get(str(row["sample_key"]), np.array([], dtype=np.float32))
        if x.size == 0:
            continue
        orig = originals[data_index][-x.shape[0] :]
        recon = reconstructions[data_index][-x.shape[0] :]
        for feature_idx, feature_name in enumerate(feature_names):
            ax = axes[row_idx, feature_idx]
            ax.plot(x, orig[:, feature_idx], linewidth=1.8, label="original")
            ax.plot(x, recon[:, feature_idx], linewidth=1.6, linestyle="--", label="reconstruction")
            if row_idx == 0:
                ax.set_title(feature_name)
            if feature_idx == 0:
                ax.set_ylabel(f"cluster {cluster_id}\n{row['sample_key']}\nloss={row['reconstruction_loss']:.3f}")
            configure_day_axis(ax, x)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right")
    fig.tight_layout(rect=[0, 0, 0.98, 0.98])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
'''
if old_examples not in text:
    raise SystemExit('old examples function not found')
text = text.replace(old_examples, new_examples, 1)

old_main_chunk = '''    originals = denormalize(prepared.sequences, means, stds)
    reconstructions = denormalize(reconstructed_norm, means, stds)

    viz_dir = run_dir / "visualizations"
'''
new_main_chunk = '''    originals = denormalize(prepared.sequences, means, stds)
    reconstructions = denormalize(reconstructed_norm, means, stds)
    day_axes = load_day_axes(metadata, args)

    viz_dir = run_dir / "visualizations"
'''
if old_main_chunk not in text:
    raise SystemExit('main chunk not found')
text = text.replace(old_main_chunk, new_main_chunk, 1)

old_calls = '''        plot_cluster_prototype(cluster_id, cluster_df, originals, reconstructions, prepared.masks, metadata["feature_names"], prototype_path)
        plot_cluster_examples(cluster_id, top_examples, originals, reconstructions, prepared.masks, metadata["feature_names"], examples_path)
'''
new_calls = '''        plot_cluster_prototype(cluster_id, cluster_df, originals, reconstructions, metadata["feature_names"], day_axes, prototype_path)
        plot_cluster_examples(cluster_id, top_examples, originals, reconstructions, metadata["feature_names"], day_axes, examples_path)
'''
if old_calls not in text:
    raise SystemExit('plot call block not found')
text = text.replace(old_calls, new_calls, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'updated {path}')
