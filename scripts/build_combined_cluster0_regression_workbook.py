from pathlib import Path

import pandas as pd

BASE = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT = BASE / 'output'
DOWNLOADS = Path('/Users/adammarq/Downloads')
TARGET = DOWNLOADS / 'cluster0_combined_regression_workbook.xlsx'

full_static = pd.read_csv(OUTPUT / 'cluster_0_regression_dataset_full_window.csv')
days_static = pd.read_csv(OUTPUT / 'cluster_0_regression_dataset_days7_66.csv')
full_static_intl = pd.read_csv(OUTPUT / 'full_window_regression_with_international_not_europe.csv')
days_static_intl = pd.read_csv(OUTPUT / 'days7_66_regression_with_international_not_europe.csv')
time_summary = pd.read_csv(OUTPUT / 'cluster_0_timeseries_regression_summary.csv')
time_features = pd.read_csv(OUTPUT / 'cluster_0_timeseries_similarity_features.csv')
intl_summary = pd.read_csv(DOWNLOADS / 'international_not_europe_regression_results.csv')
pattern_summary = pd.read_excel(OUTPUT / 'cluster_0_pattern_workbook.xlsx', sheet_name='summary')

with pd.ExcelWriter(TARGET) as writer:
    intl_summary.to_excel(writer, sheet_name='intl_not_europe_summary', index=False)
    time_summary.to_excel(writer, sheet_name='timeseries_model_summary', index=False)
    pattern_summary.to_excel(writer, sheet_name='cluster0_pattern_summary', index=False)
    full_static_intl.to_excel(writer, sheet_name='full_window_static', index=False)
    days_static_intl.to_excel(writer, sheet_name='days7_66_static', index=False)
    time_features.to_excel(writer, sheet_name='timeseries_features', index=False)

    summary_rows = []
    for name, df in [('full_window_static', full_static_intl), ('days7_66_static', days_static_intl)]:
        summary_rows.append({
            'dataset': name,
            'rows': int(len(df)),
            'cluster0_rows': int(df['is_cluster_0'].sum()) if 'is_cluster_0' in df.columns else None,
            'international_not_europe_rows': int(df['is_international_not_europe'].sum()) if 'is_international_not_europe' in df.columns else None,
        })
    for _, row in intl_summary.iterrows():
        summary_rows.append({
            'dataset': f"intl_regression::{row['run_name']}",
            'rows': int(row['sample_count']),
            'cluster0_rows': int(row['cluster0_count']),
            'international_not_europe_rows': None,
        })
    for _, row in time_summary.iterrows():
        summary_rows.append({
            'dataset': f"timeseries::{row['model_name']}",
            'rows': int(row['sample_count']),
            'cluster0_rows': int(row['cluster0_count']),
            'international_not_europe_rows': None,
        })
    pd.DataFrame(summary_rows).to_excel(writer, sheet_name='overview', index=False)

print(f'saved_xlsx={TARGET}')
