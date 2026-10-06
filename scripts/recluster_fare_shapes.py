#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sklearn.cluster import KMeans


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--embeddings",
        default="/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder/fare_shape_embeddings.csv",
    )
    parser.add_argument(
        "--cluster-count",
        type=int,
        default=5,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    embeddings_path = Path(args.embeddings)
    base = embeddings_path.parent
    df = pd.read_csv(embeddings_path)
    latent_cols = [col for col in df.columns if col.startswith("latent_")]
    if not latent_cols:
        raise ValueError("No latent columns found in embeddings CSV.")

    cluster_col = f"shape_cluster_k{args.cluster_count}"
    out_embeddings = base / f"fare_shape_embeddings_k{args.cluster_count}.csv"
    out_summary = base / f"fare_shape_cluster_summary_k{args.cluster_count}.csv"

    kmeans = KMeans(n_clusters=args.cluster_count, random_state=42, n_init=20)
    df[cluster_col] = kmeans.fit_predict(df[latent_cols])
    df = df.sort_values([cluster_col, "reconstruction_loss", "sample_key"]).reset_index(drop=True)

    summary = (
        df.groupby(cluster_col)
        .agg(
            sample_count=("sample_key", "count"),
            avg_reconstruction_loss=("reconstruction_loss", "mean"),
            min_reconstruction_loss=("reconstruction_loss", "min"),
        )
        .reset_index()
        .sort_values("sample_count", ascending=False)
    )

    df.to_csv(out_embeddings, index=False)
    summary.to_csv(out_summary, index=False)

    print(f"saved_embeddings={out_embeddings}")
    print(f"saved_summary={out_summary}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
