import json
import math
from pathlib import Path

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

BASE = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT = BASE / 'output'
DOWNLOADS = Path('/Users/adammarq/Downloads')

MOUNTAIN_DESTINATIONS = {'ASE', 'BZN', 'JAC', 'SUN'}
WESTERN_US_ORIGINS = {'LAX', 'OAK', 'SFO', 'SJC', 'DFW', 'PHX', 'DEN', 'SLC', 'SEA', 'PDX'}
EAST_COAST_ORIGINS = {'ATL', 'CLT', 'DCA', 'DTW', 'EWR', 'FLL', 'JFK', 'LGA', 'MIA', 'PHL'}

RUNS = [
    {
        'name': 'full_window',
        'dataset_csv': OUTPUT / 'cluster_0_regression_dataset_full_window.csv',
        'metadata_path': OUTPUT / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3' / 'fare_shape_autoencoder_metadata.json',
        'min_days': 1,
    },
    {
        'name': 'days7_66',
        'dataset_csv': OUTPUT / 'cluster_0_regression_dataset_days7_66.csv',
        'metadata_path': OUTPUT / 'fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48' / 'fare_shape_autoencoder_metadata.json',
        'min_days': 7,
    },
]

BOOLEAN_VARS = [
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

NUMERIC_VARS = [
    'duration_minutes',
    'departure_minutes',
    'reconstruction_loss',
    'price_value',
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
    'avg_price_diff_vs_cheapest_competing_nonstop',
    'avg_price_diff_vs_cheapest_other_airline_competing_nonstop',
    'avg_price_diff_vs_cheapest_same_airline_alternate_nonstop',
]

AVG_SOURCE_VARS = [
    'price_diff_vs_cheapest_competing_nonstop',
    'price_diff_vs_cheapest_other_airline_competing_nonstop',
    'price_diff_vs_cheapest_same_airline_alternate_nonstop',
]


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
    df['is_western_us_to_mountains'] = (
        origin.isin(WESTERN_US_ORIGINS) & destination.isin(MOUNTAIN_DESTINATIONS)
    ).astype(int)
    df['is_east_coast_to_mountains'] = (
        origin.isin(EAST_COAST_ORIGINS) & destination.isin(MOUNTAIN_DESTINATIONS)
    ).astype(int)
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
    grouped = grouped.rename(columns={
        'price_diff_vs_cheapest_competing_nonstop': 'avg_price_diff_vs_cheapest_competing_nonstop',
        'price_diff_vs_cheapest_other_airline_competing_nonstop': 'avg_price_diff_vs_cheapest_other_airline_competing_nonstop',
        'price_diff_vs_cheapest_same_airline_alternate_nonstop': 'avg_price_diff_vs_cheapest_same_airline_alternate_nonstop',
    })
    return grouped


def run_boolean_regression(df: pd.DataFrame, variable: str) -> dict:
    model_df = df[['is_cluster_0', variable]].dropna().copy()
    if model_df.empty or model_df['is_cluster_0'].nunique() < 2:
        raise ValueError(f'Insufficient classes for {variable}')
    x = model_df[[variable]]
    y = model_df['is_cluster_0']
    model = LogisticRegression(max_iter=1000)
    model.fit(x, y)
    probs = model.predict_proba(x)[:, 1]
    coef = float(model.coef_[0][0])
    share_cluster0 = float(model_df.loc[model_df['is_cluster_0'] == 1, variable].mean())
    share_non_cluster0 = float(model_df.loc[model_df['is_cluster_0'] == 0, variable].mean())
    return {
        'variable_type': 'boolean',
        'variable': variable,
        'sample_count': int(len(model_df)),
        'cluster0_count': int(model_df['is_cluster_0'].sum()),
        'cluster0_mean': share_cluster0,
        'non_cluster0_mean': share_non_cluster0,
        'difference': share_cluster0 - share_non_cluster0,
        'coefficient': coef,
        'odds_ratio': float(math.exp(coef)),
        'auc_in_sample': float(roc_auc_score(y, probs)),
    }


def run_numeric_regression(df: pd.DataFrame, variable: str) -> dict:
    model_df = df[['is_cluster_0', variable]].dropna().copy()
    if model_df.empty or model_df['is_cluster_0'].nunique() < 2:
        raise ValueError(f'Insufficient classes for {variable}')
    x = model_df[[variable]]
    y = model_df['is_cluster_0']
    pipe = Pipeline([
        ('scaler', StandardScaler()),
        ('model', LogisticRegression(max_iter=1000)),
    ])
    pipe.fit(x, y)
    probs = pipe.predict_proba(x)[:, 1]
    coef = float(pipe.named_steps['model'].coef_[0][0])
    return {
        'variable_type': 'numeric',
        'variable': variable,
        'sample_count': int(len(model_df)),
        'cluster0_count': int(model_df['is_cluster_0'].sum()),
        'cluster0_mean': float(model_df.loc[model_df['is_cluster_0'] == 1, variable].mean()),
        'non_cluster0_mean': float(model_df.loc[model_df['is_cluster_0'] == 0, variable].mean()),
        'difference': float(model_df.loc[model_df['is_cluster_0'] == 1, variable].mean() - model_df.loc[model_df['is_cluster_0'] == 0, variable].mean()),
        'coefficient_std_scaled': coef,
        'auc_in_sample': float(roc_auc_score(y, probs)),
    }


all_results = []
augmented_datasets = {}
for run in RUNS:
    df = pd.read_csv(run['dataset_csv'])
    avg_df = load_avg_price_diff_features(run['metadata_path'], run['min_days'])
    df = df.merge(avg_df, on='sample_key', how='left')
    df = add_derived_route_flags(df)
    augmented_datasets[run['name']] = df

    for variable in BOOLEAN_VARS:
        if variable in df.columns:
            result = run_boolean_regression(df, variable)
            result['run_name'] = run['name']
            all_results.append(result)

    for variable in NUMERIC_VARS:
        if variable in df.columns:
            result = run_numeric_regression(df, variable)
            result['run_name'] = run['name']
            all_results.append(result)

results_df = pd.DataFrame(all_results)
results_df = results_df.sort_values(['run_name', 'variable_type', 'auc_in_sample'], ascending=[True, True, False])

out_csv = OUTPUT / 'univariate_static_regression_results_with_routeflags.csv'
out_xlsx = DOWNLOADS / 'univariate_static_regression_results_with_routeflags.xlsx'
results_df.to_csv(out_csv, index=False)
with pd.ExcelWriter(out_xlsx) as writer:
    results_df.to_excel(writer, sheet_name='summary', index=False)
    for run_name, df in augmented_datasets.items():
        df.to_excel(writer, sheet_name=run_name[:31], index=False)

print(results_df.to_string(index=False))
print(f'saved_csv={out_csv}')
print(f'saved_xlsx={out_xlsx}')
