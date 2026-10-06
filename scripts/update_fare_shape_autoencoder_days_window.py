from pathlib import Path

path = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py")
text = path.read_text()
original = text

anchor = '    parser.add_argument("--horizon-days", type=int, default=66)\n'
insertion = anchor + '    parser.add_argument("--min-days-until-departure", type=int, default=1)\n'

if 'parser.add_argument("--min-days-until-departure", type=int, default=1)' not in text:
    if anchor not in text:
        raise SystemExit('anchor not found for parser insertion')
    text = text.replace(anchor, insertion, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'updated {path}')
