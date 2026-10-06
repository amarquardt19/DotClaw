#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--cluster-id', type=int, required=True)
    parser.add_argument('--min-days', type=int, required=True)
    parser.add_argument('--prototype-steps', type=int, default=48)
    parser.add_argument('--change-threshold', type=float, default=10.0)
    return parser.parse_args()


def prepare_raw(metadata: dict, min_days: int) -> pd.DataFrame:
    raw = pd.read_excel(metadata['workbook'], sheet_name=metadata['sheet_name'])
    if 'matched_target_flight' in raw.columns:
        raw = raw[raw['matched_target_flight'].fillna(False).astype(bool)].copy()
    raw = raw[raw['route_service_label'].notna()].copy()
    raw['query_timestamp'] = pd.to_datetime(raw['query_timestamp'], errors='coerce')
    raw['days_until_departure'] = pd.to_numeric(raw['days_until_departure'], errors='coerce')
    raw['price_value'] = pd.to_numeric(raw['price_value'], errors='coerce')
    raw = raw[raw['days_until_departure'].notna()].copy()
    raw = raw[raw['days_until_departure'] >= min_days].copy()
    raw = raw[raw['price_value'].notna()].copy()
    raw = raw.sort_values(['route_service_label', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])
    raw = raw.rename(columns={'route_service_label': 'sample_key'})
    return raw


def resample_1d(values: np.ndarray, target_steps: int) -> np.ndarray:
    if values.size == 0:
        return np.zeros(target_steps, dtype=float)
    if values.size == target_steps:
        return values.astype(float).copy()
    if values.size == 1:
        return np.repeat(float(values[0]), target_steps)
    source_x = np.linspace(1.0, target_steps, num=values.size)
    target_x = np.arange(1, target_steps + 1)
    return np.interp(target_x, source_x, values.astype(float))


def build_cluster_sequences(raw: pd.DataFrame, embeddings: pd.DataFrame, cluster_id: int, prototype_steps: int) -> tuple[np.ndarray, list[str]]:
    cluster_keys = embeddings.loc[embeddings['shape_cluster'] == cluster_id, 'sample_key'].astype(str).tolist()
    series_list = []
    kept_keys = []
    for sample_key, group in raw[raw['sample_key'].isin(cluster_keys)].groupby('sample_key'):
        prices = group['price_value'].dropna().to_numpy(dtype=float)
        if prices.size < 2:
            continue
        series_list.append(resample_1d(prices, prototype_steps))
        kept_keys.append(sample_key)
    if not series_list:
        return np.empty((0, prototype_steps), dtype=float), []
    return np.stack(series_list), kept_keys


def plot_change_visual(run_dir: Path, cluster_id: int, sequences: np.ndarray, threshold: float) -> tuple[Path, Path, pd.DataFrame]:
    deltas = np.diff(sequences, axis=1)
    mean_delta = deltas.mean(axis=0)
    state_values = np.where(deltas > threshold, 1, np.where(deltas < -threshold, -1, 0))
    avg_state = np.where(mean_delta > threshold, 1, np.where(mean_delta < -threshold, -1, 0))
    share_up = (state_values == 1).mean(axis=0)
    share_same = (state_values == 0).mean(axis=0)
    share_down = (state_values == -1).mean(axis=0)
    steps = np.arange(2, sequences.shape[1] + 1)

    viz_dir = run_dir / 'visualizations'
    viz_dir.mkdir(parents=True, exist_ok=True)
    delta_plot = viz_dir / f'cluster_{cluster_id}_avg_price_delta.png'
    discrete_plot = viz_dir / f'cluster_{cluster_id}_avg_price_delta_state.png'
    share_plot = viz_dir / f'cluster_{cluster_id}_price_change_shares.png'

    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(steps, mean_delta, color='tab:blue', linewidth=2.2, label='average period-to-period fare change')
    ax.axhline(threshold, color='tab:green', linestyle='--', linewidth=1.5, label=f'+${threshold:.0f} threshold')
    ax.axhline(0, color='gray', linestyle='-', linewidth=1.0)
    ax.axhline(-threshold, color='tab:red', linestyle='--', linewidth=1.5, label=f'-${threshold:.0f} threshold')
    ax.fill_between(steps, threshold, mean_delta, where=mean_delta >= threshold, color='tab:green', alpha=0.18)
    ax.fill_between(steps, -threshold, mean_delta, where=mean_delta <= -threshold, color='tab:red', alpha=0.18)
    ax.set_title(f'cluster {cluster_id} average period-to-period fare change')
    ax.set_xlabel('pseudo-time step t')
    ax.set_ylabel('average fare change ($)')
    ax.grid(True, alpha=0.25)
    ax.legend(loc='best')
    fig.tight_layout()
    fig.savefig(delta_plot, dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 3.8))
    ax.step(steps, avg_state, where='mid', color='tab:purple', linewidth=2.4)
    ax.scatter(steps, avg_state, color='tab:purple', s=28, zorder=3)
    ax.set_title(f'cluster {cluster_id} average fare-change state')
    ax.set_xlabel('pseudo-time step t')
    ax.set_ylabel('state')
    ax.set_yticks([-1, 0, 1])
    ax.set_yticklabels(['-1 decrease', '0 same', '+1 increase'])
    ax.set_ylim(-1.3, 1.3)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(discrete_plot, dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 4.8))
    ax.stackplot(steps, share_down, share_same, share_up, labels=['decrease', 'same', 'increase'], colors=['tab:red', 'lightgray', 'tab:green'], alpha=0.85)
    ax.set_ylim(0, 1)
    ax.set_title(f'cluster {cluster_id} share of flights by fare-change state')
    ax.set_xlabel('pseudo-time step t')
    ax.set_ylabel('share of flights')
    ax.grid(True, axis='y', alpha=0.25)
    ax.legend(loc='upper right')
    fig.tight_layout()
    fig.savefig(share_plot, dpi=150)
    plt.close(fig)

    summary = pd.DataFrame({
        'pseudo_step': steps,
        'avg_price_delta': mean_delta,
        'avg_price_delta_state': avg_state,
        'share_decrease': share_down,
        'share_same': share_same,
        'share_increase': share_up,
    })
    summary_path = viz_dir / f'cluster_{cluster_id}_price_change_summary.csv'
    summary.to_csv(summary_path, index=False)
    return delta_plot, discrete_plot, share_plot, summary


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir)
    metadata = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())
    embeddings = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')
    raw = prepare_raw(metadata, args.min_days)
    sequences, kept_keys = build_cluster_sequences(raw, embeddings, args.cluster_id, args.prototype_steps)
    if sequences.shape[0] == 0:
        raise ValueError(f'No cluster sequences found for cluster {args.cluster_id}')
    delta_plot, discrete_plot, share_plot, summary = plot_change_visual(run_dir, args.cluster_id, sequences, args.change_threshold)
    print(f'cluster_samples={len(kept_keys)}')
    print(f'saved_delta_plot={delta_plot}')
    print(f'saved_discrete_plot={discrete_plot}')
    print(f'saved_share_plot={share_plot}')
    print(summary.head(10).to_string(index=False))


if __name__ == '__main__':
    main()
