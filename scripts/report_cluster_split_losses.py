#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import train_fare_shape_autoencoder as base


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--min-days-until-departure', type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir)
    metadata = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())

    prepare_args = SimpleNamespace(
        workbook=metadata['workbook'],
        sheet_name=metadata['sheet_name'],
        output_dir=str(run_dir),
        feature_profile=metadata['feature_profile'],
        model_profile='compact3',
        horizon_days=66,
        min_days_until_departure=args.min_days_until_departure,
        max_seq_len=metadata['max_seq_len'],
        min_observations=8,
        periodized_cum_accel_steps=metadata.get('periodized_cum_accel_steps', 48),
        fare_change_threshold_minor=metadata.get('fare_change_threshold_minor', 2.5),
        fare_change_threshold=metadata.get('fare_change_threshold', 10.0),
        batch_size=32,
        epochs=1,
        learning_rate=1e-3,
        latent_dim=metadata['latent_dim'],
        cluster_count=metadata['cluster_count_requested'],
        train_fraction=0.7,
        val_fraction=0.15,
        test_fraction=0.15,
        seed=42,
        split_strategy=metadata.get('split_strategy', 'random'),
    )

    prepared = base.prepare_data(prepare_args)
    train_idx, val_idx, test_idx, _ = base.make_splits(prepare_args, prepared)

    embeddings = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')
    embeddings['split'] = 'train'
    embeddings.loc[val_idx, 'split'] = 'val'
    embeddings.loc[test_idx, 'split'] = 'test'

    summary = (
        embeddings.groupby(['split', 'shape_cluster'], as_index=False)
        .agg(sample_count=('sample_key', 'count'), avg_reconstruction_loss=('reconstruction_loss', 'mean'))
        .sort_values(['split', 'sample_count', 'avg_reconstruction_loss'], ascending=[True, False, True])
    )
    print(summary.to_string(index=False))


if __name__ == '__main__':
    main()
