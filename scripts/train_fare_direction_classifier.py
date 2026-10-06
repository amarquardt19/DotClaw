#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

import train_fare_shape_autoencoder as training

BASE = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT = BASE / 'output'
DOWNLOADS = Path('/Users/adammarq/Downloads')

MOUNTAIN_DESTINATIONS = {'ASE', 'BZN', 'JAC', 'SUN'}
WESTERN_US_ORIGINS = {'LAX', 'OAK', 'SFO', 'SJC', 'DFW', 'PHX', 'DEN', 'SLC', 'SEA', 'PDX'}
EAST_COAST_ORIGINS = {'ATL', 'CLT', 'DCA', 'DTW', 'EWR', 'FLL', 'JFK', 'LGA', 'MIA', 'PHL'}
DELTA_CHANGEABLE_FOR_FEE = {'Delta'}
NOT_CHANGEABLE_BASIC = {'United', 'American', 'Alaska', 'JetBlue', 'Air Canada'}

RUNS = [
    {
        'name': 'full_window',
        'run_dir': OUTPUT / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3',
        'min_days': 1,
    },
    {
        'name': 'days7_66',
        'run_dir': OUTPUT / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48',
        'min_days': 7,
    },
]

NUMERIC_FEATURES = [
    'price_value',
    'duration_minutes',
    'departure_minutes',
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
    'avg_price_diff_vs_cheapest_competing_nonstop',
    'avg_price_diff_vs_cheapest_other_airline_competing_nonstop',
    'avg_price_diff_vs_cheapest_same_airline_alternate_nonstop',
    'initial_price_value',
]

BOOLEAN_FEATURES = [
    'is_weekend_departure',
    'holiday_window_flag',
    'fourth_of_july_flag',
    'labor_day_flag',
    'is_domestic_route',
    'is_north_america_route',
    'is_western_us_to_mountains',
    'is_east_coast_to_mountains',
    'is_east_coast_route',
    'is_europe_route',
    'is_latam_route',
    'is_canada_route',
    'is_caribbean_route',
    'basic_changeable_for_fee_policy',
    'basic_not_changeable_policy',
]

CATEGORICAL_FEATURES = [
    'airline',
    'departure_dow',
]


def representative_rows(raw: pd.DataFrame) -> pd.DataFrame:
    data = raw.copy()
    if 'query_timestamp' in data.columns:
        data['query_timestamp'] = pd.to_datetime(data['query_timestamp'], errors='coerce')
    if 'days_until_departure' in data.columns:
        data['days_until_departure'] = pd.to_numeric(data['days_until_departure'], errors='coerce')
    return (
        data.sort_values(['route_service_label', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])
        .groupby('route_service_label', as_index=False)
        .last()
    )


def add_derived_route_flags(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ['is_europe_route', 'is_latam_route', 'is_canada_route', 'is_caribbean_route']:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
    df['is_domestic_route'] = (
        (df['is_europe_route'] == 0)
        & (df['is_latam_route'] == 0)
        & (df['is_canada_route'] == 0)
        & (df['is_caribbean_route'] == 0)
    ).astype(int)
    df['is_north_america_route'] = (
        (df['is_domestic_route'] == 1)
        | (df['is_canada_route'] == 1)
        | (df['is_caribbean_route'] == 1)
    ).astype(int)
    route_parts = df['sample_key'].astype(str).str.extract(r'^(?P<origin>[A-Z]{3})-(?P<destination>[A-Z]{3})')
    origin = route_parts['origin'].fillna('')
    destination = route_parts['destination'].fillna('')
    df['is_western_us_to_mountains'] = (origin.isin(WESTERN_US_ORIGINS) & destination.isin(MOUNTAIN_DESTINATIONS)).astype(int)
    df['is_east_coast_to_mountains'] = (origin.isin(EAST_COAST_ORIGINS) & destination.isin(MOUNTAIN_DESTINATIONS)).astype(int)
    df['is_east_coast_route'] = origin.isin(EAST_COAST_ORIGINS).astype(int)
    return df


def load_avg_price_diff_features(raw: pd.DataFrame) -> pd.DataFrame:
    data = raw.copy()
    data['sample_key'] = data['route_service_label']
    cols = [
        'price_diff_vs_cheapest_competing_nonstop',
        'price_diff_vs_cheapest_other_airline_competing_nonstop',
        'price_diff_vs_cheapest_same_airline_alternate_nonstop',
    ]
    for col in cols:
        data[col] = pd.to_numeric(data[col], errors='coerce')
    grouped = data.groupby('sample_key', as_index=False)[cols].mean()
    return grouped.rename(columns={
        'price_diff_vs_cheapest_competing_nonstop': 'avg_price_diff_vs_cheapest_competing_nonstop',
        'price_diff_vs_cheapest_other_airline_competing_nonstop': 'avg_price_diff_vs_cheapest_other_airline_competing_nonstop',
        'price_diff_vs_cheapest_same_airline_alternate_nonstop': 'avg_price_diff_vs_cheapest_same_airline_alternate_nonstop',
    })


def build_direction_targets(raw: pd.DataFrame) -> pd.DataFrame:
    data = raw.copy()
    data['query_timestamp'] = pd.to_datetime(data['query_timestamp'], errors='coerce')
    data['days_until_departure'] = pd.to_numeric(data['days_until_departure'], errors='coerce')
    data['price_value'] = pd.to_numeric(data['price_value'], errors='coerce')
    data = data[data['route_service_label'].notna() & data['days_until_departure'].notna()].copy()
    data = data.sort_values(['route_service_label', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True])

    rows = []
    for sample_key, group in data.groupby('route_service_label'):
        prices = group['price_value'].dropna().to_numpy(dtype=float)
        if prices.size == 0:
            continue
        initial_price = float(prices[0])
        final_price = float(prices[-1])
        net_price_change = final_price - initial_price
        direction_label = 1 if net_price_change > 0 else (-1 if net_price_change < 0 else 0)
        rows.append({
            'sample_key': sample_key,
            'initial_price_value': initial_price,
            'final_price_value': final_price,
            'net_price_change': net_price_change,
            'direction_label': direction_label,
        })
    return pd.DataFrame(rows)


def build_prepare_args(meta: dict, run_dir: Path, min_days: int) -> SimpleNamespace:
    return SimpleNamespace(
        workbook=meta['workbook'],
        sheet_name=meta['sheet_name'],
        output_dir=str(run_dir),
        feature_profile=meta['feature_profile'],
        model_profile=meta.get('model_profile', 'deep4'),
        horizon_days=66,
        min_days_until_departure=min_days,
        max_seq_len=int(meta.get('max_seq_len', 66)),
        min_observations=8,
        periodized_cum_accel_steps=int(meta.get('periodized_cum_accel_steps', 48)),
        batch_size=32,
        epochs=1,
        learning_rate=1e-3,
        latent_dim=meta['latent_dim'],
        cluster_count=meta.get('cluster_count_requested', 3),
        train_fraction=0.7,
        val_fraction=0.15,
        test_fraction=0.15,
        seed=42,
        split_strategy=meta.get('split_strategy', 'duration_stratified'),
    )


def run_one(run: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    meta = json.loads((run['run_dir'] / 'fare_shape_autoencoder_metadata.json').read_text())
    raw = pd.read_excel(meta['workbook'], sheet_name=meta['sheet_name'])
    if 'matched_target_flight' in raw.columns:
        raw = raw[raw['matched_target_flight'].fillna(False).astype(bool)].copy()
    raw = raw[raw['route_service_label'].notna()].copy()
    raw['days_until_departure'] = pd.to_numeric(raw['days_until_departure'], errors='coerce')
    raw = raw[raw['days_until_departure'].notna()].copy()
    raw = raw[raw['days_until_departure'] >= run['min_days']].copy()

    target_df = build_direction_targets(raw)
    rep_df = representative_rows(raw).rename(columns={'route_service_label': 'sample_key'})
    avg_df = load_avg_price_diff_features(raw)

    features = rep_df.merge(target_df, on='sample_key', how='inner').merge(avg_df, on='sample_key', how='left')
    features['basic_changeable_for_fee_policy'] = features['airline'].isin(DELTA_CHANGEABLE_FOR_FEE).astype(int)
    features['basic_not_changeable_policy'] = features['airline'].isin(NOT_CHANGEABLE_BASIC).astype(int)
    features = add_derived_route_flags(features)

    for col in NUMERIC_FEATURES:
        if col in features.columns:
            features[col] = pd.to_numeric(features[col], errors='coerce')
    for col in BOOLEAN_FEATURES:
        if col in features.columns:
            features[col] = pd.to_numeric(features[col], errors='coerce').fillna(0).astype(int)
    for col in CATEGORICAL_FEATURES:
        if col in features.columns:
            features[col] = features[col].fillna('unknown').astype(str)

    prepare_args = build_prepare_args(meta, run['run_dir'], run['min_days'])
    prepared = training.prepare_data(prepare_args)
    train_idx, val_idx, test_idx, _ = training.make_splits(prepare_args, prepared)
    split_lookup = {}
    for idx in train_idx:
        split_lookup[prepared.sample_keys[idx]] = 'train'
    for idx in val_idx:
        split_lookup[prepared.sample_keys[idx]] = 'val'
    for idx in test_idx:
        split_lookup[prepared.sample_keys[idx]] = 'test'
    features['split'] = features['sample_key'].map(split_lookup)
    features = features[features['split'].notna()].copy()

    feature_cols = [
        col for col in NUMERIC_FEATURES + BOOLEAN_FEATURES + CATEGORICAL_FEATURES
        if col in features.columns
    ]
    train_df = features[features['split'] == 'train'].copy()
    val_df = features[features['split'] == 'val'].copy()
    test_df = features[features['split'] == 'test'].copy()

    numeric_cols = [col for col in NUMERIC_FEATURES if col in feature_cols]
    boolean_cols = [col for col in BOOLEAN_FEATURES if col in feature_cols]
    categorical_cols = [col for col in CATEGORICAL_FEATURES if col in feature_cols]

    preprocessor = ColumnTransformer(
        transformers=[
            ('num', Pipeline([('imputer', SimpleImputer(strategy='median')), ('scaler', StandardScaler())]), numeric_cols),
            ('bool', Pipeline([('imputer', SimpleImputer(strategy='most_frequent'))]), boolean_cols),
            ('cat', Pipeline([('imputer', SimpleImputer(strategy='most_frequent')), ('onehot', OneHotEncoder(handle_unknown='ignore'))]), categorical_cols),
        ]
    )

    model = Pipeline([
        ('preprocessor', preprocessor),
        ('classifier', LogisticRegression(max_iter=3000, multi_class='multinomial')),
    ])
    model.fit(train_df[feature_cols], train_df['direction_label'])

    metric_rows = []
    for split_name, split_df in [('train', train_df), ('val', val_df), ('test', test_df)]:
        preds = model.predict(split_df[feature_cols])
        metric_rows.append({
            'run_name': run['name'],
            'split': split_name,
            'sample_count': int(len(split_df)),
            'accuracy': float(accuracy_score(split_df['direction_label'], preds)),
            'class_counts': json.dumps(split_df['direction_label'].value_counts().sort_index().to_dict()),
            'confusion_matrix': json.dumps(confusion_matrix(split_df['direction_label'], preds, labels=[-1, 0, 1]).tolist()),
        })

    preds_test = model.predict(test_df[feature_cols])
    report = classification_report(test_df['direction_label'], preds_test, labels=[-1, 0, 1], output_dict=True, zero_division=0)
    report_rows = []
    for label_name in ['-1', '0', '1', 'accuracy', 'macro avg', 'weighted avg']:
        if label_name not in report:
            continue
        row = report[label_name]
        if isinstance(row, dict):
            report_rows.append({
                'run_name': run['name'],
                'label': label_name,
                'precision': row.get('precision'),
                'recall': row.get('recall'),
                'f1_score': row.get('f1-score'),
                'support': row.get('support'),
            })
        else:
            report_rows.append({
                'run_name': run['name'],
                'label': label_name,
                'precision': None,
                'recall': None,
                'f1_score': row,
                'support': None,
            })

    return pd.DataFrame(metric_rows), pd.DataFrame(report_rows)


def main() -> None:
    all_metrics = []
    all_reports = []
    for run in RUNS:
        metrics_df, report_df = run_one(run)
        all_metrics.append(metrics_df)
        all_reports.append(report_df)

    metrics = pd.concat(all_metrics, ignore_index=True)
    reports = pd.concat(all_reports, ignore_index=True)
    metrics_csv = OUTPUT / 'fare_direction_classifier_metrics.csv'
    reports_csv = OUTPUT / 'fare_direction_classifier_test_report.csv'
    workbook = DOWNLOADS / 'fare_direction_classifier_results.xlsx'
    metrics.to_csv(metrics_csv, index=False)
    reports.to_csv(reports_csv, index=False)
    with pd.ExcelWriter(workbook) as writer:
        metrics.to_excel(writer, sheet_name='metrics', index=False)
        reports.to_excel(writer, sheet_name='test_report', index=False)
    print(metrics.to_string(index=False))
    print(reports.to_string(index=False))
    print(f'saved_metrics={metrics_csv}')
    print(f'saved_report={reports_csv}')
    print(f'saved_workbook={workbook}')


if __name__ == '__main__':
    main()
