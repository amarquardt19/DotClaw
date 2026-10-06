from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/visualize_fare_shape_autoencoder.py')
text = path.read_text()
old = '        min_observations=cli_args.min_observations,\n        batch_size=cli_args.batch_size,\n'
new = '        min_observations=cli_args.min_observations,\n        periodized_cum_accel_steps=metadata.get("periodized_cum_accel_steps", cli_args.prototype_steps),\n        batch_size=cli_args.batch_size,\n'
if old not in text:
    raise SystemExit('target block not found')
path.write_text(text.replace(old, new, 1))
print('fixed visualizer prepare args for periodized cumulative accel')
