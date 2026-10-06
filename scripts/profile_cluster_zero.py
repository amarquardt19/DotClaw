import json
from pathlib import Path

import pandas as pd

run_dir = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3')
meta = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())
emb = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')
df = pd.read_excel(meta['workbook'], sheet_name=meta['sheet_name'])

if 'matched_target_flight' in df.columns:
    df = df[df['matched_target_flight'].fillna(False).astype(bool)].copy()

df = df[df['route_service_label'].notna()].copy()

if 'query_timestamp' in df.columns:
    df['query_timestamp'] = pd.to_datetime(df['query_timestamp'], errors='coerce')
if 'days_until_departure' in df.columns:
    df['days_until_departure'] = pd.to_numeric(df['days_until_departure'], errors='coerce')

rep = (
    df.sort_values(['route_service_label', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])
    .groupby('route_service_label', as_index=False)
    .last()
)
merged = emb.merge(rep, left_on='sample_key', right_on='route_service_label', how='left')
cluster0 = merged[merged['shape_cluster'] == 0].copy()

print(f'cluster0_count={len(cluster0)} total={len(merged)} share={len(cluster0) / len(merged):.3f}')

num_cols = [
    c for c in [
        'duration_minutes',
        'departure_minutes',
        'price_value',
        'days_until_departure',
        'competing_nonstop_same_time_bucket_count',
        'competing_nonstop_other_airlines_same_time_bucket_count',
        'alternate_nonstop_any_airline_count',
    ] if c in merged.columns
]
for col in num_cols:
    merged[col] = pd.to_numeric(merged[col], errors='coerce')
    cluster0[col] = pd.to_numeric(cluster0[col], errors='coerce')
    print(
        f'NUM {col}: '
        f'cluster0_mean={cluster0[col].mean():.2f} '
        f'overall_mean={merged[col].mean():.2f} '
        f'cluster0_median={cluster0[col].median():.2f} '
        f'overall_median={merged[col].median():.2f}'
    )

cat_cols = [c for c in ['airline', 'origin', 'destination', 'time_of_day_bucket', 'holiday_season', 'departure_dow'] if c in merged.columns]
for col in cat_cols:
    overall = merged[col].fillna('unknown').value_counts(normalize=True)
    c0 = cluster0[col].fillna('unknown').value_counts(normalize=True)
    print(f'CAT {col}')
    for val, pct in c0.head(6).items():
        base = overall.get(val, 0.0)
        lift = pct / base if base else float('inf')
        print(f'  {val}: cluster0_share={pct:.3f} overall_share={base:.3f} lift={lift:.2f}')

flag_cols = [c for c in ['is_weekend_departure', 'holiday_window_flag', 'fourth_of_july_flag', 'labor_day_flag', 'is_europe_route', 'is_latam_route', 'is_canada_route', 'is_caribbean_route'] if c in merged.columns]
for col in flag_cols:
    overall = pd.to_numeric(merged[col], errors='coerce')
    c0 = pd.to_numeric(cluster0[col], errors='coerce')
    print(f'FLAG {col}: cluster0_rate={c0.mean():.3f} overall_rate={overall.mean():.3f}')

print('TOP_CLUSTER0_SAMPLE_KEYS')
for key, count in cluster0['sample_key'].value_counts().head(15).items():
    print(f'  {key}: {count}')
