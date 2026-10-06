import json
from pathlib import Path

import numpy as np
import pandas as pd

import train_fare_shape_autoencoder as training

PROJECT_ROOT = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
RUN_DIR = PROJECT_ROOT / 'output' / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48'
OUTPUT_CSV = PROJECT_ROOT / 'output' / 'cluster_0_regression_dataset_days7_66.csv'
OUTPUT_XLSX = PROJECT_ROOT / 'output' / 'cluster_0_regression_dataset_days7_66.xlsx'

NUMERIC_FACTORS = [
    'price_value',
    'duration_minutes',
    'departure_minutes',
    'reconstruction_loss',
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
]

BOOLEAN_FACTORS = [
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
]

CATEGORICAL_FACTORS = [
    'airline',
    'departure_dow',
]

DELTA_CHANGEABLE_FOR_FEE = {'Delta'}
NOT_CHANGEABLE_BASIC = {'United', 'American', 'Alaska', 'JetBlue', 'Air Canada'}


def make_args(meta: dict):
    return type(
        'Args',
        (),
        {
            'workbook': meta['workbook'],
            'sheet_name': meta['sheet_name'],
            'output_dir': str(RUN_DIR),
            'feature_profile': meta['feature_profile'],
            'model_profile': meta.get('model_profile', 'compact3'),
            'horizon_days': 66,
            'min_days_until_departure': meta.get('min_days_until_departure', 7),
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


meta = json.loads((RUN_DIR / 'fare_shape_autoencoder_metadata.json').read_text())
embeddings = pd.read_csv(RUN_DIR / 'fare_shape_embeddings.csv')
args = make_args(meta)
prepared = training.prepare_data(args)
train_idx, val_idx, test_idx, _ = training.make_splits(args, prepared)
split = np.array([''] * len(prepared.sample_keys), dtype=object)
split[train_idx] = 'train'
split[val_idx] = 'val'
split[test_idx] = 'test'
embeddings['split'] = split
embeddings['is_cluster_0'] = (embeddings['shape_cluster'] == 0).astype(int)

raw = pd.read_excel(meta['workbook'], sheet_name=meta['sheet_name'])
if 'matched_target_flight' in raw.columns:
    raw = raw[raw['matched_target_flight'].fillna(False).astype(bool)].copy()
raw = raw[raw['route_service_label'].notna()].copy()
merged = embeddings.merge(representative_rows(raw), left_on='sample_key', right_on='route_service_label', how='left')

merged['basic_changeable_for_fee_policy'] = merged['airline'].isin(DELTA_CHANGEABLE_FOR_FEE).astype(int)
merged['basic_not_changeable_policy'] = merged['airline'].isin(NOT_CHANGEABLE_BASIC).astype(int)

for col in NUMERIC_FACTORS:
    if col in merged.columns:
        merged[col] = pd.to_numeric(merged[col], errors='coerce')
for col in BOOLEAN_FACTORS:
    if col in merged.columns:
        merged[col] = pd.to_numeric(merged[col], errors='coerce').fillna(0).astype(int)
for col in CATEGORICAL_FACTORS:
    if col in merged.columns:
        merged[col] = merged[col].fillna('unknown').astype(str)

selected_columns = [
    'sample_key',
    'shape_cluster',
    'is_cluster_0',
    'split',
] + [col for col in NUMERIC_FACTORS if col in merged.columns] + [col for col in BOOLEAN_FACTORS if col in merged.columns] + [col for col in CATEGORICAL_FACTORS if col in merged.columns]

regression_df = merged[selected_columns].copy()
regression_df.to_csv(OUTPUT_CSV, index=False)
with pd.ExcelWriter(OUTPUT_XLSX) as writer:
    regression_df.to_excel(writer, sheet_name='regression_data', index=False)
    pd.DataFrame({
        'numeric_factors': pd.Series([col for col in NUMERIC_FACTORS if col in regression_df.columns]),
        'boolean_factors': pd.Series([col for col in BOOLEAN_FACTORS if col in regression_df.columns]),
        'categorical_factors': pd.Series([col for col in CATEGORICAL_FACTORS if col in regression_df.columns]),
    }).to_excel(writer, sheet_name='factor_lists', index=False)

print(f'saved_csv={OUTPUT_CSV}')
print(f'saved_xlsx={OUTPUT_XLSX}')
print('selected_columns=' + ', '.join(regression_df.columns))
