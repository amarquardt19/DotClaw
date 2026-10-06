import math
from pathlib import Path

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

BASE = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
RUNS = {
    'full_window': BASE / 'output' / 'cluster_0_regression_dataset_full_window.csv',
    'days7_66': BASE / 'output' / 'cluster_0_regression_dataset_days7_66.csv',
}
DOWNLOADS = Path('/Users/adammarq/Downloads')

results = []
for run_name, path in RUNS.items():
    df = pd.read_csv(path)
    for col in ['is_europe_route', 'is_latam_route', 'is_canada_route', 'is_caribbean_route']:
        if col not in df.columns:
            df[col] = 0
    df['is_international_not_europe'] = (
        (df['is_latam_route'].fillna(0) == 1)
        | (df['is_canada_route'].fillna(0) == 1)
        | (df['is_caribbean_route'].fillna(0) == 1)
    ).astype(int)

    model_df = df[['is_cluster_0', 'is_international_not_europe']].dropna().copy()
    x = model_df[['is_international_not_europe']]
    y = model_df['is_cluster_0']

    model = LogisticRegression(max_iter=1000)
    model.fit(x, y)
    probs = model.predict_proba(x)[:, 1]
    coef = float(model.coef_[0][0])
    intercept = float(model.intercept_[0])
    auc = float(roc_auc_score(y, probs))

    cluster0_share = float(model_df.loc[model_df['is_cluster_0'] == 1, 'is_international_not_europe'].mean())
    non_cluster0_share = float(model_df.loc[model_df['is_cluster_0'] == 0, 'is_international_not_europe'].mean())

    df.to_csv(BASE / 'output' / f'{run_name}_regression_with_international_not_europe.csv', index=False)

    results.append(
        {
            'run_name': run_name,
            'sample_count': int(len(model_df)),
            'cluster0_count': int(model_df['is_cluster_0'].sum()),
            'cluster0_share_international_not_europe': cluster0_share,
            'non_cluster0_share_international_not_europe': non_cluster0_share,
            'logit_intercept': intercept,
            'logit_coefficient': coef,
            'odds_ratio': float(math.exp(coef)),
            'auc_in_sample': auc,
        }
    )

results_df = pd.DataFrame(results)
out_csv = DOWNLOADS / 'international_not_europe_regression_results.csv'
out_xlsx = DOWNLOADS / 'international_not_europe_regression_results.xlsx'
results_df.to_csv(out_csv, index=False)
with pd.ExcelWriter(out_xlsx) as writer:
    results_df.to_excel(writer, sheet_name='summary', index=False)
    for run_name in RUNS:
        df = pd.read_csv(BASE / 'output' / f'{run_name}_regression_with_international_not_europe.csv')
        df.to_excel(writer, sheet_name=run_name[:31], index=False)

print(results_df.to_string(index=False))
print(f'saved_csv={out_csv}')
print(f'saved_xlsx={out_xlsx}')
