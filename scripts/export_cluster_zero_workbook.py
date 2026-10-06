import json
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT_PATH = PROJECT_ROOT / 'output' / 'fare_shape_cluster_0_line_items.xlsx'

RUNS = [
    {
        'sheet_name': 'full_window_cluster_0',
        'run_dir': PROJECT_ROOT / 'output' / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3',
    },
    {
        'sheet_name': 'days7_66_cluster_0',
        'run_dir': PROJECT_ROOT / 'output' / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48',
    },
]

DISPLAY_COLUMNS = [
    'sample_key',
    'shape_cluster',
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
    'alternate_nonstop_any_airline_count',
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

summary_rows = []

with pd.ExcelWriter(OUTPUT_PATH) as writer:
    for spec in RUNS:
        run_dir = spec['run_dir']
        meta = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())
        embeddings = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')
        workbook = Path(meta['workbook'])
        raw = pd.read_excel(workbook, sheet_name=meta['sheet_name'])

        if 'matched_target_flight' in raw.columns:
            raw = raw[raw['matched_target_flight'].fillna(False).astype(bool)].copy()
        raw = raw[raw['route_service_label'].notna()].copy()

        if 'query_timestamp' in raw.columns:
            raw['query_timestamp'] = pd.to_datetime(raw['query_timestamp'], errors='coerce')
        if 'departure_date' in raw.columns:
            raw['departure_date'] = pd.to_datetime(raw['departure_date'], errors='coerce')
        if 'days_until_departure' in raw.columns:
            raw['days_until_departure'] = pd.to_numeric(raw['days_until_departure'], errors='coerce')

        representative = (
            raw.sort_values(['route_service_label', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])
            .groupby('route_service_label', as_index=False)
            .last()
        )

        merged = embeddings.merge(representative, left_on='sample_key', right_on='route_service_label', how='left')
        cluster_zero = merged[merged['shape_cluster'] == 0].copy()
        keep_cols = [col for col in DISPLAY_COLUMNS if col in cluster_zero.columns]
        cluster_zero = cluster_zero[keep_cols].sort_values(['reconstruction_loss', 'sample_key'], ascending=[True, True])
        cluster_zero.to_excel(writer, sheet_name=spec['sheet_name'][:31], index=False)

        summary_rows.append(
            {
                'run_name': spec['sheet_name'],
                'run_dir': str(run_dir),
                'cluster_id': 0,
                'sample_count': int(cluster_zero.shape[0]),
                'avg_reconstruction_loss': float(cluster_zero['reconstruction_loss'].mean()) if not cluster_zero.empty else None,
                'min_reconstruction_loss': float(cluster_zero['reconstruction_loss'].min()) if not cluster_zero.empty else None,
                'max_reconstruction_loss': float(cluster_zero['reconstruction_loss'].max()) if not cluster_zero.empty else None,
            }
        )

    pd.DataFrame(summary_rows).to_excel(writer, sheet_name='summary', index=False)

print(f'saved_workbook={OUTPUT_PATH}')
