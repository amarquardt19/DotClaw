import json
from pathlib import Path

import numpy as np
import pandas as pd

import train_fare_shape_autoencoder as training

PROJECT_ROOT = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT_XLSX = PROJECT_ROOT / 'output' / 'fare_shape_cluster_0_line_items_detailed.xlsx'
OUTPUT_CSV = PROJECT_ROOT / 'output' / 'fare_shape_cluster_0_comparison_summary.csv'

RUNS = [
    {
        'name': 'full_window_cluster_0',
        'run_dir': PROJECT_ROOT / 'output' / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3',
        'horizon_days': 66,
        'default_min_days': 1,
    },
    {
        'name': 'days7_66_cluster_0',
        'run_dir': PROJECT_ROOT / 'output' / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48',
        'horizon_days': 66,
        'default_min_days': 7,
    },
]

DISPLAY_COLUMNS = [
    'sample_key',
    'shape_cluster',
    'split',
    'reconstruction_loss',
    'origin',
    'destination',
    'airline',
    'time_of_day_bucket',
    'departure_dow',
    'holiday_season',
    'departure_date',
    'duration_minutes',
    'departure_minutes',
    'price_value',
    'days_until_departure',
    'competing_nonstop_same_time_bucket_count',
    'competing_nonstop_other_airlines_same_time_bucket_count',
    'alternate_nonstop_same_airline_count',
    'alternate_nonstop_different_airline_count',
    'alternate_nonstop_any_airline_count',
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
    'price_diff_vs_cheapest_different_airline_alternate_nonstop',
    'is_weekend_departure',
    'holiday_window_flag',
    'fourth_of_july_flag',
    'labor_day_flag',
    'is_europe_route',
    'is_latam_route',
    'is_canada_route',
    'is_caribbean_route',
    'route_service_label',
]


def make_args(meta: dict, run_dir: Path, horizon_days: int, default_min_days: int):
    return type(
        'Args',
        (),
        {
            'workbook': meta['workbook'],
            'sheet_name': meta['sheet_name'],
            'output_dir': str(run_dir),
            'feature_profile': meta['feature_profile'],
            'model_profile': meta.get('model_profile', 'compact3'),
            'horizon_days': horizon_days,
            'min_days_until_departure': meta.get('min_days_until_departure', default_min_days),
            'max_seq_len': int(meta.get('max_seq_len', 66)),
            'min_observations': 8,
            'periodized_cum_accel_steps': int(meta.get('periodized_cum_accel_steps', 48)),
            'batch_size': 32,
            'epochs': 1,
            'learning_rate': 1e-3,
            'latent_dim': meta['latent_dim'],
            'cluster_count': meta.get('cluster_count_requested', 3),
            'train_fraction': 0.7,
            'val_fraction': 0.15,
            'test_fraction': 0.15,
            'seed': 42,
            'split_strategy': meta.get('split_strategy', 'duration_stratified'),
        },
    )


def representative_rows(raw: pd.DataFrame) -> pd.DataFrame:
    data = raw.copy()
    if 'query_timestamp' in data.columns:
        data['query_timestamp'] = pd.to_datetime(data['query_timestamp'], errors='coerce')
    if 'departure_date' in data.columns:
        data['departure_date'] = pd.to_datetime(data['departure_date'], errors='coerce')
    if 'days_until_departure' in data.columns:
        data['days_until_departure'] = pd.to_numeric(data['days_until_departure'], errors='coerce')
    return (
        data.sort_values(['route_service_label', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])
        .groupby('route_service_label', as_index=False)
        .last()
    )


summary_rows: list[dict[str, object]] = []

with pd.ExcelWriter(OUTPUT_XLSX) as writer:
    for spec in RUNS:
        run_dir = spec['run_dir']
        meta = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())
        embeddings = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')

        args = make_args(meta, run_dir, spec['horizon_days'], spec['default_min_days'])
        prepared = training.prepare_data(args)
        train_idx, val_idx, test_idx, _ = training.make_splits(args, prepared)
        split = np.array([''] * len(prepared.sample_keys), dtype=object)
        split[train_idx] = 'train'
        split[val_idx] = 'val'
        split[test_idx] = 'test'
        embeddings['split'] = split

        raw = pd.read_excel(meta['workbook'], sheet_name=meta['sheet_name'])
        if 'matched_target_flight' in raw.columns:
            raw = raw[raw['matched_target_flight'].fillna(False).astype(bool)].copy()
        raw = raw[raw['route_service_label'].notna()].copy()
        merged = embeddings.merge(representative_rows(raw), left_on='sample_key', right_on='route_service_label', how='left')

        cluster_zero = merged[merged['shape_cluster'] == 0].copy()
        keep_cols = [col for col in DISPLAY_COLUMNS if col in cluster_zero.columns]
        cluster_zero = cluster_zero[keep_cols].sort_values(['split', 'reconstruction_loss', 'sample_key'])
        cluster_zero.to_excel(writer, sheet_name=spec['name'][:31], index=False)

        numeric_compare_cols = [
            'competing_nonstop_same_time_bucket_count',
            'competing_nonstop_other_airlines_same_time_bucket_count',
            'alternate_nonstop_same_airline_count',
            'alternate_nonstop_different_airline_count',
            'alternate_nonstop_any_airline_count',
            'price_diff_vs_cheapest_competing_nonstop',
            'price_diff_vs_cheapest_other_airline_competing_nonstop',
            'price_diff_vs_cheapest_same_airline_alternate_nonstop',
            'price_diff_vs_cheapest_different_airline_alternate_nonstop',
        ]
        for col in numeric_compare_cols:
            if col in merged.columns:
                merged[col] = pd.to_numeric(merged[col], errors='coerce')
                cluster_zero[col] = pd.to_numeric(cluster_zero[col], errors='coerce')
                summary_rows.append({
                    'run_name': spec['name'],
                    'metric': col,
                    'cluster_0_mean': float(cluster_zero[col].mean()),
                    'total_mean': float(merged[col].mean()),
                    'cluster_0_median': float(cluster_zero[col].median()),
                    'total_median': float(merged[col].median()),
                })

        for flag_col in ['is_europe_route', 'is_latam_route', 'is_canada_route', 'is_caribbean_route']:
            if flag_col in merged.columns:
                all_flag = pd.to_numeric(merged[flag_col], errors='coerce').fillna(0)
                c0_flag = pd.to_numeric(cluster_zero[flag_col], errors='coerce').fillna(0)
                summary_rows.append({
                    'run_name': spec['name'],
                    'metric': flag_col,
                    'cluster_0_mean': float(c0_flag.mean()),
                    'total_mean': float(all_flag.mean()),
                    'cluster_0_median': float(c0_flag.median()),
                    'total_median': float(all_flag.median()),
                    'cluster_0_count_true': int((c0_flag > 0).sum()),
                    'total_count_true': int((all_flag > 0).sum()),
                })

        split_counts = cluster_zero['split'].value_counts()
        summary_rows.append({
            'run_name': spec['name'],
            'metric': 'cluster_0_count',
            'cluster_0_mean': float(cluster_zero.shape[0]),
            'total_mean': float(merged.shape[0]),
            'cluster_0_median': float(split_counts.get('train', 0)),
            'total_median': float(split_counts.get('val', 0)),
            'cluster_0_count_true': int(split_counts.get('test', 0)),
        })

    pd.DataFrame(summary_rows).to_excel(writer, sheet_name='comparison_summary', index=False)

pd.DataFrame(summary_rows).to_csv(OUTPUT_CSV, index=False)
print(f'saved_workbook={OUTPUT_XLSX}')
print(f'saved_summary_csv={OUTPUT_CSV}')
