import json
from pathlib import Path

import numpy as np
import pandas as pd

import train_fare_shape_autoencoder as t

run_dir = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66')
meta = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())

class Args:
    workbook = meta['workbook']
    sheet_name = meta['sheet_name']
    output_dir = str(run_dir)
    feature_profile = meta['feature_profile']
    model_profile = meta.get('model_profile', 'compact3')
    horizon_days = 66
    min_days_until_departure = meta.get('min_days_until_departure', 7)
    max_seq_len = 66
    min_observations = 8
    batch_size = 32
    epochs = 1
    learning_rate = 1e-3
    latent_dim = meta['latent_dim']
    cluster_count = meta.get('cluster_count_requested', 3)
    train_fraction = 0.7
    val_fraction = 0.15
    test_fraction = 0.15
    seed = 42
    split_strategy = meta.get('split_strategy', 'duration_stratified')

prepared = t.prepare_data(Args)
train_idx, val_idx, test_idx, _ = t.make_splits(Args, prepared)
split = np.array([''] * len(prepared.sample_keys), dtype=object)
split[train_idx] = 'train'
split[val_idx] = 'val'
split[test_idx] = 'test'

emb = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')
emb['split'] = split
summary = emb.groupby(['shape_cluster', 'split'], as_index=False).agg(
    sample_count=('sample_key', 'size'),
    avg_reconstruction_loss=('reconstruction_loss', 'mean'),
)
summary.to_csv(run_dir / 'fare_shape_cluster_split_summary.csv', index=False)

print('RUN: days7_66 cumulative accel main')
for cluster_id in [0, 1, 2]:
    print(f'cluster {cluster_id}')
    rows = summary[summary['shape_cluster'] == cluster_id].set_index('split')
    for split_name in ['train', 'val', 'test']:
        if split_name in rows.index:
            row = rows.loc[split_name]
            print(f"  {split_name}: count={int(row['sample_count'])} avg_loss={row['avg_reconstruction_loss']:.6f}")
        else:
            print(f'  {split_name}: count=0 avg_loss=NA')
