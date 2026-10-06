#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import train_fare_shape_autoencoder as training
import visualize_fare_shape_autoencoder as visualization


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--clusters", nargs="+", type=int, required=True)
    parser.add_argument("--examples-per-cluster", type=int, default=3)
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--output-subdir", default="visualizations_test_only")
    return parser.parse_args()


def build_split_labels(metadata: dict, args: argparse.Namespace, prepared: training.PreparedAutoencoderData) -> np.ndarray:
    split_args = argparse.Namespace(
        workbook=metadata["workbook"],
        sheet_name=metadata["sheet_name"],
        output_dir=str(args.run_dir),
        feature_profile=metadata["feature_profile"],
        model_profile=metadata.get("model_profile", "deep4"),
        horizon_days=args.horizon_days,
        min_days_until_departure=metadata.get("min_days_until_departure", 1),
        max_seq_len=args.max_seq_len,
        min_observations=args.min_observations,
        batch_size=args.batch_size,
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
    train_idx, val_idx, test_idx, _ = training.make_splits(split_args, prepared)
    split = np.array([""] * len(prepared.sample_keys), dtype=object)
    split[train_idx] = "train"
    split[val_idx] = "val"
    split[test_idx] = "test"
    return split


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir)
    metadata = json.loads((run_dir / "fare_shape_autoencoder_metadata.json").read_text())
    embeddings = pd.read_csv(run_dir / "fare_shape_embeddings.csv")

    prepare_args = visualization.build_prepare_args(metadata, args)
    prepared = training.prepare_data(prepare_args)
    if prepared.sample_keys != embeddings["sample_key"].tolist():
        order = {key: idx for idx, key in enumerate(prepared.sample_keys)}
        embeddings["data_index"] = embeddings["sample_key"].map(order)
        embeddings = embeddings.dropna(subset=["data_index"]).copy()
        embeddings["data_index"] = embeddings["data_index"].astype(int)
    else:
        embeddings["data_index"] = np.arange(len(embeddings))

    split_labels = build_split_labels(metadata, args, prepared)
    embeddings["split"] = embeddings["data_index"].map(lambda idx: split_labels[int(idx)])
    test_embeddings = embeddings[embeddings["split"] == "test"].copy()

    model = visualization.load_model(metadata, prepared.sequences.shape[2], prepared.sequences.shape[1], run_dir / "fare_shape_autoencoder.pt")
    reconstructed_norm = visualization.reconstruct_sequences(model, prepared.sequences, args.batch_size)
    means = np.array(metadata["feature_means"], dtype=np.float32)
    stds = np.array(metadata["feature_stds"], dtype=np.float32)
    originals = visualization.denormalize(prepared.sequences, means, stds)
    reconstructions = visualization.denormalize(reconstructed_norm, means, stds)

    viz_dir = run_dir / args.output_subdir
    viz_dir.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, object]] = []

    for cluster_id in args.clusters:
        cluster_df = test_embeddings[test_embeddings["shape_cluster"] == cluster_id].sort_values("reconstruction_loss", ascending=True).copy()
        if cluster_df.empty:
            continue
        top_examples = cluster_df.head(args.examples_per_cluster)
        prototype_path = viz_dir / f"cluster_{cluster_id}_test_only_prototype.png"
        examples_path = viz_dir / f"cluster_{cluster_id}_test_only_examples.png"
        visualization.plot_cluster_prototype(cluster_id, cluster_df, originals, reconstructions, prepared.masks, metadata["feature_names"], prototype_path)
        visualization.plot_cluster_examples(cluster_id, top_examples, originals, reconstructions, prepared.masks, metadata["feature_names"], examples_path)
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
    print(f"saved_test_only_visualizations={viz_dir}")


if __name__ == "__main__":
    main()
