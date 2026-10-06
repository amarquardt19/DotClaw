import json
from pathlib import Path

import numpy as np
import pandas as pd

import train_fare_shape_autoencoder as training

PROJECT_ROOT = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT_XLSX = PROJECT_ROOT / 'output' / 'cluster_0_pattern_workbook.xlsx'

RUNS = [
    {
        'name': 'full_window',
        'run_dir': PROJECT_ROOT / 'output' / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3',
        'default_min_days': 1,
    },
    {
        'name': 'days7_66',
        'run_dir': PROJECT_ROOT / 'output' / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48',
        'default_min_days': 7,
    },
]

STATIC_COLUMNS = [
    'sample_key',
    'shape_cluster',
    'split',
    'reconstruction_loss',
    'price_value',
    'duration_minutes',
    'departure_minutes',
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
    'is_weekend_departure',
    'holiday_window_flag',
    'fourth_of_july_flag',
    'labor_day_flag',
    'is_europe_route',
    'is_latam_route',
    'is_canada_route',
    'is_caribbean_route',
    'basic_changeable_for_fee_policy',
    'basic_not_changeable_policy',
    'airline',
    'origin',
    'destination',
    'departure_dow',
    'time_of_day_bucket',
    'holiday_season',
    'route_service_label',
]

TIME_SERIES_COLUMNS = [
    'sample_key',
    'shape_cluster',
    'split',
    'query_timestamp',
    'departure_date',
    'days_until_departure',
    'airline',
    'origin',
    'destination',
    'departure_dow',
    'duration_minutes',
    'departure_minutes',
    'price_value',
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
    'is_weekend_departure',
    'holiday_window_flag',
    'fourth_of_july_flag',
    'labor_day_flag',
    'is_europe_route',
    'is_latam_route',
    'is_canada_route',
    'is_caribbean_route',
    'basic_changeable_for_fee_policy',
    'basic_not_changeable_policy',
    'time_of_day_bucket',
    'holiday_season',
    'route_service_label',
]

DELTA_CHANGEABLE_FOR_FEE = {'Delta'}
NOT_CHANGEABLE_BASIC = {'United', 'American', 'Alaska', 'JetBlue', 'Air Canada'}


def make_args(meta: dict, run_dir: Path, default_min_days: int):
    return type(
        'Args',
        (),
        {
            'workbook': meta['workbook'],
            'sheet_name': meta['sheet_name'],
            'output_dir': str(run_dir),
            'feature_profile': meta['feature_profile'],
            'model_profile': meta.get('model_profile', 'compact3'),
            'horizon_days': 66,
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


def prepare_raw(raw: pd.DataFrame) -> pd.DataFrame:
    data = raw.copy()
    if 'matched_target_flight' in data.columns:
        data = data[data['matched_target_flight'].fillna(False).astype(bool)].copy()
    data = data[data['route_service_label'].notna()].copy()
    if 'query_timestamp' in data.columns:
        data['query_timestamp'] = pd.to_datetime(data['query_timestamp'], errors='coerce')
    if 'departure_date' in data.columns:
        data['departure_date'] = pd.to_datetime(data['departure_date'], errors='coerce')
    if 'days_until_departure' in data.columns:
        data['days_until_departure'] = pd.to_numeric(data['days_until_departure'], errors='coerce')
    data['basic_changeable_for_fee_policy'] = data['airline'].isin(DELTA_CHANGEABLE_FOR_FEE).astype(int)
    data['basic_not_changeable_policy'] = data['airline'].isin(NOT_CHANGEABLE_BASIC).astype(int)
    data['sample_key'] = data['route_service_label']
    return data


summary_rows = []

with pd.ExcelWriter(OUTPUT_XLSX) as writer:
    for spec in RUNS:
        run_dir = spec['run_dir']
        meta = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())
        embeddings = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')
        args = make_args(meta, run_dir, spec['default_min_days'])
        prepared = training.prepare_data(args)
        train_idx, val_idx, test_idx, _ = training.make_splits(args, prepared)
        split = np.array([''] * len(prepared.sample_keys), dtype=object)
        split[train_idx] = 'train'
        split[val_idx] = 'val'
        split[test_idx] = 'test'
        embeddings['split'] = split

        raw = pd.read_excel(meta['workbook'], sheet_name=meta['sheet_name'])
        raw_prepared = prepare_raw(raw)
        representative = representative_rows(raw_prepared)
        representative = representative.drop(columns=['sample_key'], errors='ignore')

        static_df = embeddings.merge(representative, left_on='sample_key', right_on='route_service_label', how='left')
        cluster0_static = static_df[static_df['shape_cluster'] == 0].copy()
        static_cols = [col for col in STATIC_COLUMNS if col in cluster0_static.columns]
        cluster0_static = cluster0_static[static_cols].sort_values(['split', 'reconstruction_loss', 'sample_key'])

        cluster0_keys = set(cluster0_static['sample_key'].dropna().astype(str))
        cluster0_time = raw_prepared[raw_prepared['sample_key'].astype(str).isin(cluster0_keys)].copy()
        cluster0_time = cluster0_time.merge(
            embeddings[['sample_key', 'shape_cluster', 'split']],
            on='sample_key',
            how='left',
        )
        cluster0_time = cluster0_time[cluster0_time['shape_cluster'] == 0].copy()
        time_cols = [col for col in TIME_SERIES_COLUMNS if col in cluster0_time.columns]
        cluster0_time = cluster0_time[time_cols].sort_values(['sample_key', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])

        cluster0_static.to_excel(writer, sheet_name=f"{spec['name']}_static"[:31], index=False)
        cluster0_time.to_excel(writer, sheet_name=f"{spec['name']}_time"[:31], index=False)

        summary_rows.append(
            {
                'run_name': spec['name'],
                'cluster0_service_rows': len(cluster0_static),
                'cluster0_time_rows': len(cluster0_time),
                'cluster0_unique_services': cluster0_static['sample_key'].nunique(),
            }
        )

    pd.DataFrame(summary_rows).to_excel(writer, sheet_name='summary', index=False)

print(f'saved_xlsx={OUTPUT_XLSX}')
