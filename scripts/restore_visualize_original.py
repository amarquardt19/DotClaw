from pathlib import Path

content = '''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch

import train_fare_shape_autoencoder as training


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--clusters", nargs="+", type=int, default=[0, 2, 3, 4])
    parser.add_argument("--examples-per-cluster", type=int, default=3)
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    return parser.parse_args()


def build_prepare_args(metadata: dict, cli_args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        workbook=metadata["workbook"],
        sheet_name=metadata["sheet_name"],
        output_dir=str(cli_args.run_dir),
        feature_profile=metadata["feature_profile"],
        model_profile=metadata.get("model_profile", "deep4"),
        horizon_days=cli_args.horizon_days,
        min_days_until_departure=metadata.get("min_days_until_departure", 1),
        max_seq_len=cli_args.max_seq_len,
        min_observations=cli_args.min_observations,
        batch_size=cli_args.batch_size,
        epochs=1,
        learning_rate=1e-3,
        latent_dim=metadata["latent_dim"],
        cluster_count=metadata.get("cluster_count_requested", 5),
        train_fraction=0.7,
        val_fraction=0.15,
        test_fraction=0.15,
        seed=42,
        split_strategy=metadata.get("split_strategy", "random"),
    )


def load_model(metadata: dict, feature_dim: int, seq_len: int, model_path: Path) -> torch.nn.Module:
    model_profile = metadata.get("model_profile", "deep4")
    model_cls = training.CompactTemporalConvAutoencoder if model_profile == "compact3" else training.TemporalConvAutoencoder
    model = model_cls(feature_dim=feature_dim, latent_dim=metadata["latent_dim"], seq_len=seq_len)
    state = torch.load(model_path, map_location="cpu")
    model.load_state_dict(state)
    model.eval()
    return model


def reconstruct_sequences(model: torch.nn.Module, sequences: np.ndarray, batch_size: int) -> np.ndarray:
    outputs: list[np.ndarray] = []
    with torch.no_grad():
        for start in range(0, len(sequences), batch_size):
            batch = torch.tensor(sequences[start : start + batch_size], dtype=torch.float32)
            reconstruction, _ = model(batch)
            outputs.append(reconstruction.detach().cpu().numpy())
    return np.concatenate(outputs, axis=0)


def denormalize(values: np.ndarray, means: np.ndarray, stds: np.ndarray) -> np.ndarray:
    return values * stds.reshape(1, 1, -1) + means.reshape(1, 1, -1)


def make_time_axis(mask: np.ndarray, seq_len: int) -> np.ndarray:
    observed_idx = np.flatnonzero(mask > 0)
    if observed_idx.size == 0:
        return np.arange(seq_len)
    start = int(observed_idx[0])
    end = int(observed_idx[-1]) + 1
    return np.arange(start, end)


def plot_cluster_prototype(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, masks: np.ndarray, feature_names: list[str], output_path: Path) -> None:
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


def plot_cluster_examples(cluster_id: int, cluster_df: pd.DataFrame, originals: np.ndarray, reconstructions: np.ndarray, masks: np.ndarray, feature_names: list[str], output_path: Path) -> None:
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
                ax.set_ylabel(f"cluster {cluster_id}\\n{row['sample_key']}\\nloss={row['reconstruction_loss']:.3f}")
            ax.grid(True, alpha=0.25)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right")
    fig.tight_layout(rect=[0, 0, 0.98, 0.98])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir)
    metadata = json.loads((run_dir / "fare_shape_autoencoder_metadata.json").read_text())
    embeddings = pd.read_csv(run_dir / "fare_shape_embeddings.csv")

    prepare_args = build_prepare_args(metadata, args)
    prepared = training.prepare_data(prepare_args)
    if prepared.sample_keys != embeddings["sample_key"].tolist():
        order = {key: idx for idx, key in enumerate(prepared.sample_keys)}
        embeddings["data_index"] = embeddings["sample_key"].map(order)
        embeddings = embeddings.dropna(subset=["data_index"]).copy()
        embeddings["data_index"] = embeddings["data_index"].astype(int)
    else:
        embeddings["data_index"] = np.arange(len(embeddings))

    model = load_model(metadata, prepared.sequences.shape[2], prepared.sequences.shape[1], run_dir / "fare_shape_autoencoder.pt")
    reconstructed_norm = reconstruct_sequences(model, prepared.sequences, args.batch_size)
    means = np.array(metadata["feature_means"], dtype=np.float32)
    stds = np.array(metadata["feature_stds"], dtype=np.float32)
    originals = denormalize(prepared.sequences, means, stds)
    reconstructions = denormalize(reconstructed_norm, means, stds)

    viz_dir = run_dir / "visualizations"
    viz_dir.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, object]] = []

    for cluster_id in args.clusters:
        cluster_df = embeddings[embeddings["shape_cluster"] == cluster_id].sort_values("reconstruction_loss", ascending=True).copy()
        if cluster_df.empty:
            continue
        top_examples = cluster_df.head(args.examples_per_cluster)
        prototype_path = viz_dir / f"cluster_{cluster_id}_prototype.png"
        examples_path = viz_dir / f"cluster_{cluster_id}_examples.png"
        plot_cluster_prototype(cluster_id, cluster_df, originals, reconstructions, prepared.masks, metadata["feature_names"], prototype_path)
        plot_cluster_examples(cluster_id, top_examples, originals, reconstructions, prepared.masks, metadata["feature_names"], examples_path)
        summary_rows.append(
            {
                "shape_cluster": cluster_id,
                "sample_count": int(cluster_df.shape[0]),
                "prototype_plot": str(prototype_path),
                "examples_plot": str(examples_path),
                "best_example": top_examples.iloc[0]["sample_key"],
                "best_example_loss": float(top_examples.iloc[0]["reconstruction_loss"]),
            }
        )

    pd.DataFrame(summary_rows).to_csv(viz_dir / "visualization_index.csv", index=False)
    print(f"saved_visualizations={viz_dir}")


if __name__ == "__main__":
    main()
'''

Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/visualize_fare_shape_autoencoder.py').write_text(content)
print('restored original visualizer')
