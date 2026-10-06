import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

PROJECT_ROOT = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT_DIR = PROJECT_ROOT / 'output'
WORKBOOK = Path('/Users/adammarq/Downloads/Flights Claw Final Project v6.xlsx')
SHEET_NAME = 0
PROTOTYPE_STEPS = 48
TARGET_VARIABLES = [
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
]
RUNS = [
    {
        'name': 'full_window',
        'embeddings_path': OUTPUT_DIR / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3' / 'fare_shape_embeddings.csv',
        'metadata_path': OUTPUT_DIR / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3' / 'fare_shape_autoencoder_metadata.json',
        'min_days': 1,
    },
    {
        'name': 'days7_66',
        'embeddings_path': OUTPUT_DIR / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48' / 'fare_shape_embeddings.csv',
        'metadata_path': OUTPUT_DIR / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48' / 'fare_shape_autoencoder_metadata.json',
        'min_days': 7,
    },
]
SUMMARY_XLSX = OUTPUT_DIR / 'cluster_0_timeseries_regression_summary.xlsx'
SUMMARY_CSV = OUTPUT_DIR / 'cluster_0_timeseries_regression_summary.csv'
FEATURES_CSV = OUTPUT_DIR / 'cluster_0_timeseries_similarity_features.csv'


def resample_1d(values: np.ndarray, target_steps: int) -> np.ndarray:
    if values.shape[0] == target_steps:
        return values.astype(float)
    if values.shape[0] == 1:
        return np.repeat(values.astype(float), target_steps)
    source_x = np.linspace(1.0, target_steps, num=values.shape[0])
    target_x = np.arange(1, target_steps + 1)
    return np.interp(target_x, source_x, values.astype(float))


def safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    if np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return 0.0
    corr = np.corrcoef(a, b)[0, 1]
    return 0.0 if np.isnan(corr) else float(corr)


def sequence_slope(values: np.ndarray) -> float:
    if values.shape[0] <= 1:
        return 0.0
    x = np.arange(values.shape[0], dtype=float)
    slope = np.polyfit(x, values.astype(float), 1)[0]
    return float(slope)


def early_late_gap(values: np.ndarray) -> float:
    if values.shape[0] == 0:
        return 0.0
    third = max(1, values.shape[0] // 3)
    early = float(np.mean(values[:third]))
    late = float(np.mean(values[-third:]))
    return late - early


def prepare_raw(meta: dict, min_days: int) -> pd.DataFrame:
    raw = pd.read_excel(meta['workbook'], sheet_name=meta['sheet_name'])
    if 'matched_target_flight' in raw.columns:
        raw = raw[raw['matched_target_flight'].fillna(False).astype(bool)].copy()
    raw = raw[raw['route_service_label'].notna()].copy()
    raw['sample_key'] = raw['route_service_label']
    raw['days_until_departure'] = pd.to_numeric(raw['days_until_departure'], errors='coerce')
    raw['query_timestamp'] = pd.to_datetime(raw.get('query_timestamp'), errors='coerce')
    raw = raw[raw['days_until_departure'].notna()].copy()
    raw = raw[raw['days_until_departure'] >= min_days].copy()
    for col in TARGET_VARIABLES + ['price_value']:
        if col in raw.columns:
            raw[col] = pd.to_numeric(raw[col], errors='coerce')
    return raw


def build_similarity_features(run_name: str, embeddings: pd.DataFrame, raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    embeddings = embeddings.copy()
    embeddings['is_cluster_0'] = (embeddings['shape_cluster'] == 0).astype(int)
    merged = embeddings[['sample_key', 'shape_cluster', 'is_cluster_0', 'reconstruction_loss']].merge(
        raw,
        on='sample_key',
        how='inner',
    )
    merged = merged.sort_values(['sample_key', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])

    prototypes: dict[str, np.ndarray] = {}
    for variable in TARGET_VARIABLES:
        prototype_series = []
        for _, group in merged[merged['shape_cluster'] == 0].groupby('sample_key'):
            series = group[variable].dropna().to_numpy(dtype=float)
            if series.size == 0:
                continue
            prototype_series.append(resample_1d(series, PROTOTYPE_STEPS))
        if not prototype_series:
            prototypes[variable] = np.zeros(PROTOTYPE_STEPS, dtype=float)
        else:
            prototypes[variable] = np.mean(np.stack(prototype_series), axis=0)

    rows = []
    for sample_key, group in merged.groupby('sample_key'):
        row = {
            'run_name': run_name,
            'sample_key': sample_key,
            'shape_cluster': int(group['shape_cluster'].iloc[0]),
            'is_cluster_0': int(group['is_cluster_0'].iloc[0]),
            'reconstruction_loss': float(group['reconstruction_loss'].iloc[0]),
            'raw_row_count': int(len(group)),
        }
        for variable in TARGET_VARIABLES:
            series = group[variable].dropna().to_numpy(dtype=float)
            if series.size == 0:
                aligned = np.zeros(PROTOTYPE_STEPS, dtype=float)
            else:
                aligned = resample_1d(series, PROTOTYPE_STEPS)
            prototype = prototypes[variable]
            diff = aligned - prototype
            row[f'{variable}__corr_to_cluster0'] = safe_corr(aligned, prototype)
            row[f'{variable}__mae_to_cluster0'] = float(np.mean(np.abs(diff)))
            row[f'{variable}__rmse_to_cluster0'] = float(np.sqrt(np.mean(diff ** 2)))
            row[f'{variable}__slope'] = sequence_slope(aligned)
            row[f'{variable}__slope_diff_vs_cluster0'] = row[f'{variable}__slope'] - sequence_slope(prototype)
            row[f'{variable}__late_minus_early'] = early_late_gap(aligned)
            row[f'{variable}__late_minus_early_diff_vs_cluster0'] = row[f'{variable}__late_minus_early'] - early_late_gap(prototype)
        rows.append(row)
    return pd.DataFrame(rows), prototypes


def run_logistic(df: pd.DataFrame, feature_columns: list[str]) -> tuple[float, pd.DataFrame]:
    model_df = df[['is_cluster_0'] + feature_columns].dropna().copy()
    if model_df['is_cluster_0'].nunique() < 2:
        raise ValueError('Need both classes for logistic regression')
    x = model_df[feature_columns]
    y = model_df['is_cluster_0']
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.3, random_state=42, stratify=y
    )
    pipeline = Pipeline([
        ('scaler', StandardScaler()),
        ('model', LogisticRegression(max_iter=5000, solver='lbfgs')),
    ])
    pipeline.fit(x_train, y_train)
    probs = pipeline.predict_proba(x_test)[:, 1]
    auc = roc_auc_score(y_test, probs)
    coefficients = pipeline.named_steps['model'].coef_[0]
    coef_df = pd.DataFrame({'feature': feature_columns, 'coefficient': coefficients})
    coef_df['abs_coefficient'] = coef_df['coefficient'].abs()
    coef_df = coef_df.sort_values('abs_coefficient', ascending=False)
    return float(auc), coef_df


all_feature_rows = []
summary_rows = []
coef_frames: dict[str, pd.DataFrame] = {}
prototype_frames: dict[str, pd.DataFrame] = {}

for run_spec in RUNS:
    metadata = json.loads(run_spec['metadata_path'].read_text())
    embeddings = pd.read_csv(run_spec['embeddings_path'])
    raw = prepare_raw(metadata, run_spec['min_days'])
    feature_df, prototypes = build_similarity_features(run_spec['name'], embeddings, raw)
    all_feature_rows.append(feature_df)

    for variable in TARGET_VARIABLES:
        prototype_frames[f"{run_spec['name']}_{variable[:25]}"] = pd.DataFrame({
            'pseudo_step': np.arange(1, PROTOTYPE_STEPS + 1),
            'prototype_value': prototypes[variable],
            'variable': variable,
            'run_name': run_spec['name'],
        })
        feature_columns = [
            f'{variable}__corr_to_cluster0',
            f'{variable}__mae_to_cluster0',
            f'{variable}__rmse_to_cluster0',
            f'{variable}__slope_diff_vs_cluster0',
            f'{variable}__late_minus_early_diff_vs_cluster0',
        ]
        auc, coef_df = run_logistic(feature_df, feature_columns)
        model_name = f"{run_spec['name']}::{variable}"
        coef_frames[model_name] = coef_df
        summary_rows.append({
            'run_name': run_spec['name'],
            'model_name': model_name,
            'variable': variable,
            'sample_count': int(feature_df.shape[0]),
            'cluster0_count': int(feature_df['is_cluster_0'].sum()),
            'auc': auc,
            'top_feature': coef_df.iloc[0]['feature'],
            'top_coefficient': float(coef_df.iloc[0]['coefficient']),
        })

    combined_features = []
    for variable in TARGET_VARIABLES:
        combined_features.extend([
            f'{variable}__corr_to_cluster0',
            f'{variable}__mae_to_cluster0',
            f'{variable}__rmse_to_cluster0',
            f'{variable}__slope_diff_vs_cluster0',
            f'{variable}__late_minus_early_diff_vs_cluster0',
        ])
    auc, coef_df = run_logistic(feature_df, combined_features)
    model_name = f"{run_spec['name']}::combined"
    coef_frames[model_name] = coef_df
    summary_rows.append({
        'run_name': run_spec['name'],
        'model_name': model_name,
        'variable': 'combined',
        'sample_count': int(feature_df.shape[0]),
        'cluster0_count': int(feature_df['is_cluster_0'].sum()),
        'auc': auc,
        'top_feature': coef_df.iloc[0]['feature'],
        'top_coefficient': float(coef_df.iloc[0]['coefficient']),
    })

all_features_df = pd.concat(all_feature_rows, ignore_index=True)
summary_df = pd.DataFrame(summary_rows).sort_values(['run_name', 'auc'], ascending=[True, False])
all_features_df.to_csv(FEATURES_CSV, index=False)
summary_df.to_csv(SUMMARY_CSV, index=False)

with pd.ExcelWriter(SUMMARY_XLSX) as writer:
    summary_df.to_excel(writer, sheet_name='model_summary', index=False)
    all_features_df.to_excel(writer, sheet_name='similarity_features', index=False)
    for name, coef_df in coef_frames.items():
        safe_name = name.replace('::', '_')[:31]
        coef_df.to_excel(writer, sheet_name=safe_name, index=False)
    for name, proto_df in prototype_frames.items():
        safe_name = f"p_{name}"[:31]
        proto_df.to_excel(writer, sheet_name=safe_name, index=False)

print(f'saved_summary_xlsx={SUMMARY_XLSX}')
print(f'saved_summary_csv={SUMMARY_CSV}')
print(f'saved_features_csv={FEATURES_CSV}')
print(summary_df.to_string(index=False))
