#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import train_fare_shape_autoencoder as training

BASE = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT = BASE / 'output'
DOWNLOADS = Path('/Users/adammarq/Downloads')

MOUNTAIN_DESTINATIONS = {'ASE', 'BZN', 'JAC', 'SUN'}
WESTERN_US_ORIGINS = {'LAX', 'OAK', 'SFO', 'SJC', 'DFW', 'PHX', 'DEN', 'SLC', 'SEA', 'PDX'}
EAST_COAST_ORIGINS = {'ATL', 'CLT', 'DCA', 'DTW', 'EWR', 'FLL', 'JFK', 'LGA', 'MIA', 'PHL'}
DELTA_CHANGEABLE_FOR_FEE = {'Delta'}
NOT_CHANGEABLE_BASIC = {'United', 'American', 'Alaska', 'JetBlue', 'Air Canada'}

BOOLEAN_VARS = [
    'is_weekend_departure', 'holiday_window_flag', 'fourth_of_july_flag', 'labor_day_flag',
    'is_domestic_route', 'is_north_america_route', 'is_western_us_to_mountains',
    'is_east_coast_to_mountains', 'is_east_coast_route', 'is_europe_route', 'is_latam_route',
    'is_canada_route', 'is_caribbean_route', 'basic_changeable_for_fee_policy', 'basic_not_changeable_policy',
]

NUMERIC_VARS = [
    'duration_minutes', 'departure_minutes', 'reconstruction_loss', 'price_value',
    'price_diff_vs_cheapest_competing_nonstop', 'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop', 'avg_price_diff_vs_cheapest_competing_nonstop',
    'avg_price_diff_vs_cheapest_other_airline_competing_nonstop', 'avg_price_diff_vs_cheapest_same_airline_alternate_nonstop',
]

AVG_SOURCE_VARS = [
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
]

NUMERIC_FACTORS = [
    'price_value', 'duration_minutes', 'departure_minutes', 'reconstruction_loss',
    'price_diff_vs_cheapest_competing_nonstop', 'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
]

BOOLEAN_FACTORS = [
    'is_weekend_departure', 'holiday_window_flag', 'fourth_of_july_flag', 'labor_day_flag',
    'is_europe_route', 'is_latam_route', 'is_canada_route', 'is_caribbean_route',
    'basic_changeable_for_fee_policy', 'basic_not_changeable_policy',
]

CATEGORICAL_FACTORS = ['airline', 'departure_dow']


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--min-days', type=int, required=True)
    parser.add_argument('--target-cluster', type=int, required=True)
    return parser.parse_args()


def representative_rows(raw: pd.DataFrame) -> pd.DataFrame:
    data = raw.copy()
    if 'query_timestamp' in data.columns:
        data['query_timestamp'] = pd.to_datetime(data['query_timestamp'], errors='coerce')
    if 'departure_date' in data.columns:
        data['departure_date'] = pd.to_datetime(data['departure_date'], errors='coerce')
    if 'days_until_departure' in data.columns:
        data['days_until_departure'] = pd.to_numeric(data['days_until_departure'], errors='coerce')
    return data.sort_values(['route_service_label', 'days_until_departure', 'query_timestamp'], ascending=[True, False, True]).groupby('route_service_label', as_index=False).last()


def add_derived_route_flags(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ['is_europe_route', 'is_latam_route', 'is_canada_route', 'is_caribbean_route']:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
    df['is_domestic_route'] = ((df['is_europe_route'] == 0) & (df['is_latam_route'] == 0) & (df['is_canada_route'] == 0) & (df['is_caribbean_route'] == 0)).astype(int)
    df['is_north_america_route'] = ((df['is_domestic_route'] == 1) | (df['is_canada_route'] == 1) | (df['is_caribbean_route'] == 1)).astype(int)
    route_parts = df['sample_key'].astype(str).str.extract(r'^(?P<origin>[A-Z]{3})-(?P<destination>[A-Z]{3})')
    origin = route_parts['origin'].fillna('')
    destination = route_parts['destination'].fillna('')
    df['is_western_us_to_mountains'] = (origin.isin(WESTERN_US_ORIGINS) & destination.isin(MOUNTAIN_DESTINATIONS)).astype(int)
    df['is_east_coast_to_mountains'] = (origin.isin(EAST_COAST_ORIGINS) & destination.isin(MOUNTAIN_DESTINATIONS)).astype(int)
    df['is_east_coast_route'] = origin.isin(EAST_COAST_ORIGINS).astype(int)
    return df


def load_avg_price_diff_features(metadata_path: Path, min_days: int) -> pd.DataFrame:
    meta = json.loads(metadata_path.read_text())
    raw = pd.read_excel(meta['workbook'], sheet_name=meta['sheet_name'])
    if 'matched_target_flight' in raw.columns:
        raw = raw[raw['matched_target_flight'].fillna(False).astype(bool)].copy()
    raw = raw[raw['route_service_label'].notna()].copy()
    raw['sample_key'] = raw['route_service_label']
    raw['days_until_departure'] = pd.to_numeric(raw['days_until_departure'], errors='coerce')
    raw = raw[raw['days_until_departure'].notna()].copy()
    raw = raw[raw['days_until_departure'] >= min_days].copy()
    for col in AVG_SOURCE_VARS:
        raw[col] = pd.to_numeric(raw[col], errors='coerce')
    grouped = raw.groupby('sample_key', as_index=False)[AVG_SOURCE_VARS].mean()
    return grouped.rename(columns={
        'price_diff_vs_cheapest_competing_nonstop': 'avg_price_diff_vs_cheapest_competing_nonstop',
        'price_diff_vs_cheapest_other_airline_competing_nonstop': 'avg_price_diff_vs_cheapest_other_airline_competing_nonstop',
        'price_diff_vs_cheapest_same_airline_alternate_nonstop': 'avg_price_diff_vs_cheapest_same_airline_alternate_nonstop',
    })


def run_boolean_regression(df: pd.DataFrame, variable: str) -> dict:
    model_df = df[['is_cluster_0', variable]].dropna().copy()
    x = model_df[[variable]]
    y = model_df['is_cluster_0']
    model = LogisticRegression(max_iter=1000)
    model.fit(x, y)
    probs = model.predict_proba(x)[:, 1]
    coef = float(model.coef_[0][0])
    return {
        'variable_type': 'boolean', 'variable': variable, 'sample_count': int(len(model_df)), 'cluster0_count': int(model_df['is_cluster_0'].sum()),
        'cluster0_mean': float(model_df.loc[model_df['is_cluster_0'] == 1, variable].mean()),
        'non_cluster0_mean': float(model_df.loc[model_df['is_cluster_0'] == 0, variable].mean()),
        'difference': float(model_df.loc[model_df['is_cluster_0'] == 1, variable].mean() - model_df.loc[model_df['is_cluster_0'] == 0, variable].mean()),
        'coefficient': coef, 'odds_ratio': float(math.exp(coef)), 'auc_in_sample': float(roc_auc_score(y, probs)),
    }


def run_numeric_regression(df: pd.DataFrame, variable: str) -> dict:
    model_df = df[['is_cluster_0', variable]].dropna().copy()
    x = model_df[[variable]]
    y = model_df['is_cluster_0']
    pipe = Pipeline([('scaler', StandardScaler()), ('model', LogisticRegression(max_iter=1000))])
    pipe.fit(x, y)
    probs = pipe.predict_proba(x)[:, 1]
    coef = float(pipe.named_steps['model'].coef_[0][0])
    return {
        'variable_type': 'numeric', 'variable': variable, 'sample_count': int(len(model_df)), 'cluster0_count': int(model_df['is_cluster_0'].sum()),
        'cluster0_mean': float(model_df.loc[model_df['is_cluster_0'] == 1, variable].mean()),
        'non_cluster0_mean': float(model_df.loc[model_df['is_cluster_0'] == 0, variable].mean()),
        'difference': float(model_df.loc[model_df['is_cluster_0'] == 1, variable].mean() - model_df.loc[model_df['is_cluster_0'] == 0, variable].mean()),
        'coefficient_std_scaled': coef, 'auc_in_sample': float(roc_auc_score(y, probs)),
    }


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir)
    metadata = json.loads((run_dir / 'fare_shape_autoencoder_metadata.json').read_text())
    embeddings = pd.read_csv(run_dir / 'fare_shape_embeddings.csv')

    prepare_args = SimpleNamespace(
        workbook=metadata['workbook'], sheet_name=metadata['sheet_name'], output_dir=str(run_dir), feature_profile=metadata['feature_profile'],
        model_profile=metadata.get('model_profile', 'compact3'), horizon_days=66, min_days_until_departure=args.min_days,
        max_seq_len=int(metadata.get('max_seq_len', 66)), min_observations=8, periodized_cum_accel_steps=int(metadata.get('periodized_cum_accel_steps', 48)),
        batch_size=32, epochs=1, learning_rate=1e-3, latent_dim=metadata['latent_dim'], cluster_count=metadata.get('cluster_count_requested', 3),
        train_fraction=0.7, val_fraction=0.15, test_fraction=0.15, seed=42, split_strategy=metadata.get('split_strategy', 'duration_stratified'),
    )
    prepared = training.prepare_data(prepare_args)
    train_idx, val_idx, test_idx, _ = training.make_splits(prepare_args, prepared)
    split = np.array([''] * len(prepared.sample_keys), dtype=object)
    split[train_idx] = 'train'; split[val_idx] = 'val'; split[test_idx] = 'test'
    embeddings['split'] = split
    embeddings['is_cluster_0'] = (embeddings['shape_cluster'] == args.target_cluster).astype(int)

    raw = pd.read_excel(metadata['workbook'], sheet_name=metadata['sheet_name'])
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

    selected_columns = ['sample_key', 'shape_cluster', 'is_cluster_0', 'split'] + [col for col in NUMERIC_FACTORS if col in merged.columns] + [col for col in BOOLEAN_FACTORS if col in merged.columns] + [col for col in CATEGORICAL_FACTORS if col in merged.columns]
    regression_df = merged[selected_columns].copy()
    regression_df = add_derived_route_flags(regression_df)
    avg_df = load_avg_price_diff_features(run_dir / 'fare_shape_autoencoder_metadata.json', args.min_days)
    regression_df = regression_df.merge(avg_df, on='sample_key', how='left')

    all_results = []
    for variable in BOOLEAN_VARS:
        if variable in regression_df.columns and regression_df[['is_cluster_0', variable]].dropna()['is_cluster_0'].nunique() == 2:
            result = run_boolean_regression(regression_df, variable)
            result['run_name'] = args.run_name
            all_results.append(result)
    for variable in NUMERIC_VARS:
        if variable in regression_df.columns and regression_df[['is_cluster_0', variable]].dropna()['is_cluster_0'].nunique() == 2:
            result = run_numeric_regression(regression_df, variable)
            result['run_name'] = args.run_name
            all_results.append(result)

    results_df = pd.DataFrame(all_results).sort_values(['variable_type', 'auc_in_sample'], ascending=[True, False])
    dataset_csv = OUTPUT / f'cluster_{args.target_cluster}_regression_dataset_{args.run_name}.csv'
    results_csv = OUTPUT / f'univariate_static_regression_results_cluster_{args.target_cluster}_{args.run_name}.csv'
    workbook = DOWNLOADS / f'univariate_static_regression_results_cluster_{args.target_cluster}_{args.run_name}.xlsx'
    regression_df.to_csv(dataset_csv, index=False)
    results_df.to_csv(results_csv, index=False)
    with pd.ExcelWriter(workbook) as writer:
        results_df.to_excel(writer, sheet_name='summary', index=False)
        regression_df.to_excel(writer, sheet_name='dataset', index=False)
    print(f'saved_dataset={dataset_csv}')
    print(f'saved_results={results_csv}')
    print(f'saved_workbook={workbook}')
    print(results_df.to_string(index=False))


if __name__ == '__main__':
    main()
