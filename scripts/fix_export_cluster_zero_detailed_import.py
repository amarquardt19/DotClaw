from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/export_cluster_zero_detailed.py')
text = path.read_text()
old = 'import scripts.train_fare_shape_autoencoder as training\n'
new = 'import train_fare_shape_autoencoder as training\n'
if old not in text:
    raise SystemExit('target import not found')
path.write_text(text.replace(old, new, 1))
print('fixed import')
