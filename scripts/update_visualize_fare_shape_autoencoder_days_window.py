from pathlib import Path

path = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/visualize_fare_shape_autoencoder.py")
text = path.read_text()
original = text

anchor = '        horizon_days=cli_args.horizon_days,\n'
insertion = anchor + '        min_days_until_departure=metadata.get("min_days_until_departure", 1),\n'

if 'min_days_until_departure=metadata.get("min_days_until_departure", 1)' not in text:
    if anchor not in text:
        raise SystemExit('anchor not found')
    text = text.replace(anchor, insertion, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'updated {path}')
