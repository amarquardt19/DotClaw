from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/visualize_fare_shape_autoencoder.py')
text = path.read_text()

old_rows = '        observed_rows.append(originals[idx][mask])'
old_recons = '        observed_recons.append(reconstructions[idx][mask])'
if old_rows not in text or old_recons not in text:
    raise SystemExit('prototype lines not found')
text = text.replace(old_rows, '        observed_rows.append(originals[idx][mask][::-1])', 1)
text = text.replace(old_recons, '        observed_recons.append(reconstructions[idx][mask][::-1])', 1)

start = text.index('def plot_cluster_examples(')
end = text.index('\n\ndef main()', start)
new_func = '''def plot_cluster_examples(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, masks: np.ndarray, feature_names: list[str], output_path: Path) -> None:
    fig, axes = plt.subplots(len(cluster_df), len(feature_names), figsize=(4 * len(feature_names), 2.8 * len(cluster_df)), sharex=False)
    if len(cluster_df) == 1:
        axes = np.array([axes])
    if len(feature_names) == 1:
        axes = axes.reshape(len(cluster_df), 1)

    for row_idx, (_, row) in enumerate(cluster_df.iterrows()):
        data_index = int(row["data_index"])
        x = make_time_axis(masks[data_index], masks.shape[1])
        orig = originals[data_index][x][::-1]
        recon = reconstructions[data_index][x][::-1]
        x = np.arange(orig.shape[0])
        for feature_idx, feature_name in enumerate(feature_names):
            ax = axes[row_idx, feature_idx]
            ax.plot(x, orig[:, feature_idx], linewidth=1.8, label="original")
            ax.plot(x, recon[:, feature_idx], linewidth=1.6, linestyle="--", label="reconstruction")
            if row_idx == 0:
                ax.set_title(feature_name)
            if feature_idx == 0:
                ax.set_ylabel(f"cluster {cluster_id}\\n{row['sample_key']}\\nloss={row['reconstruction_loss']:.3f}")
            ax.grid(True, alpha=0.25)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right")
    fig.tight_layout(rect=[0, 0, 0.98, 0.98])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
'''
text = text[:start] + new_func + text[end:]
path.write_text(text)
print('fixed visualizer direction')
