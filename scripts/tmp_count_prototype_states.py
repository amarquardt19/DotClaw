#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent))
import train_fare_shape_autoencoder as training
import visualize_fare_shape_autoencoder as viz

RUNS = [
    (
        "full_window",
        Path("output/fare_shape_autoencoder_value_delta_change_state_observed_compact3_duration_split_full_window_periodized48_thr5_rerun"),
        [0, 1],
        1,
    ),
    (
        "days7_66",
        Path("output/fare_shape_autoencoder_value_delta_change_state_observed_compact3_duration_split_days7_66_periodized48_thr5_rerun"),
        [2],
        7,
    ),
]

for label, run_dir, clusters, min_days in RUNS:
    metadata = json.loads((run_dir / "fare_shape_autoencoder_metadata.json").read_text())
    embeddings = pd.read_csv(run_dir / "fare_shape_embeddings.csv")
    cli_args = type(
        "Args",
        (),
        {
            "run_dir": str(run_dir),
            "horizon_days": 66,
            "max_seq_len": metadata.get("max_seq_len", 48),
            "min_observations": 8,
            "prototype_steps": metadata.get("periodized_cum_accel_steps", 48),
            "batch_size": 32,
        },
    )()
    prepare_args = viz.build_prepare_args(metadata, cli_args)
    prepare_args.min_days_until_departure = min_days
    prepared = training.prepare_data(prepare_args)

    if prepared.sample_keys != embeddings["sample_key"].tolist():
        order = {key: idx for idx, key in enumerate(prepared.sample_keys)}
        embeddings["data_index"] = embeddings["sample_key"].map(order)
        embeddings = embeddings.dropna(subset=["data_index"]).copy()
        embeddings["data_index"] = embeddings["data_index"].astype(int)
    else:
        embeddings["data_index"] = np.arange(len(embeddings))

    model = viz.load_model(
        metadata,
        prepared.sequences.shape[2],
        prepared.sequences.shape[1],
        run_dir / "fare_shape_autoencoder.pt",
    )
    reconstructed_norm = viz.reconstruct_sequences(model, prepared.sequences, 32)
    means = np.array(metadata["feature_means"], dtype=np.float32)
    stds = np.array(metadata["feature_stds"], dtype=np.float32)
    originals = viz.denormalize(prepared.sequences, means, stds)
    reconstructions = viz.denormalize(reconstructed_norm, means, stds)
    feature_names = metadata["feature_names"]
    delta_idx = feature_names.index("price_delta_per_day")
    prototype_steps = metadata.get("periodized_cum_accel_steps", 48)
    threshold = float(metadata.get("fare_change_threshold", 5.0))

    print(f"RUN {label}")
    for cluster in clusters:
        cluster_df = embeddings[embeddings["shape_cluster"] == cluster]
        observed_rows = []
        observed_recons = []
        for idx in cluster_df["data_index"].tolist():
            mask = prepared.masks[idx] > 0
            observed_rows.append(viz.resample_sequence(originals[idx][mask], prototype_steps))
            observed_recons.append(viz.resample_sequence(reconstructions[idx][mask], prototype_steps))
        aligned_originals = np.stack(observed_rows)
        aligned_recons = np.stack(observed_recons)
        original_delta_mean = aligned_originals[:, :, delta_idx].mean(axis=0)
        reconstruction_delta_mean = aligned_recons[:, :, delta_idx].mean(axis=0)
        original_state = np.where(original_delta_mean >= threshold, 1, np.where(original_delta_mean <= -threshold, -1, 0))
        reconstruction_state = np.where(reconstruction_delta_mean >= threshold, 1, np.where(reconstruction_delta_mean <= -threshold, -1, 0))
        print(f"cluster {cluster}")
        print(
            f"  original_prototype plus1={int((original_state == 1).sum())} zero={int((original_state == 0).sum())} minus1={int((original_state == -1).sum())}"
        )
        print(
            f"  recon_prototype    plus1={int((reconstruction_state == 1).sum())} zero={int((reconstruction_state == 0).sum())} minus1={int((reconstruction_state == -1).sum())}"
        )
