from pathlib import Path


path = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py")
text = path.read_text()
original = text

anchor = 'DEFAULT_OUTPUT_DIR = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder")\n\n'
insert = '''DEFAULT_OUTPUT_DIR = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder")

RGB_STATE_MOTION_NUMERIC_COLUMNS = [
    "price_value",
]

RGB_STATE_MOTION_BOOLEAN_COLUMNS: list[str] = []

RGB_STATE_MOTION_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
    "observed_flag",
]

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []

RGB_MOTION_ACCEL_BOOLEAN_COLUMNS: list[str] = []

RGB_MOTION_ACCEL_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
    "observed_flag",
]

'''
if "RGB_STATE_MOTION_NUMERIC_COLUMNS" not in text:
    if anchor not in text:
        raise SystemExit("top anchor not found")
    text = text.replace(anchor, insert, 1)

class_anchor = '''    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        latent = self.encode(x)
        reconstruction = self.decode(latent)
        return reconstruction, latent


def parse_args() -> argparse.Namespace:
'''
class_insert = '''    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        latent = self.encode(x)
        reconstruction = self.decode(latent)
        return reconstruction, latent


class CompactTemporalConvAutoencoder(nn.Module):
    def __init__(self, feature_dim: int, latent_dim: int, seq_len: int):
        super().__init__()
        self.feature_dim = feature_dim
        self.seq_len = seq_len
        self.encoder = nn.Sequential(
            nn.Conv1d(feature_dim, 32, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.Conv1d(32, 64, kernel_size=5, stride=2, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.Conv1d(64, 96, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(96),
        )
        with torch.no_grad():
            dummy = torch.zeros(1, feature_dim, seq_len)
            encoded = self.encoder(dummy)
            self.encoded_shape = encoded.shape[1:]
            self.flattened_dim = int(encoded.numel())
        self.to_latent = nn.Sequential(
            nn.Flatten(),
            nn.Linear(self.flattened_dim, 192),
            nn.ReLU(),
            nn.Linear(192, latent_dim),
        )
        self.from_latent = nn.Sequential(
            nn.Linear(latent_dim, 192),
            nn.ReLU(),
            nn.Linear(192, self.flattened_dim),
            nn.ReLU(),
        )
        self.decoder = nn.Sequential(
            nn.ConvTranspose1d(96, 64, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.ConvTranspose1d(64, 32, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.Conv1d(32, feature_dim, kernel_size=3, padding=1),
        )

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        encoded = self.encoder(x.transpose(1, 2))
        return self.to_latent(encoded)

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        expanded = self.from_latent(latent)
        expanded = expanded.view(latent.shape[0], *self.encoded_shape)
        decoded = self.decoder(expanded)
        if decoded.shape[-1] > self.seq_len:
            decoded = decoded[..., : self.seq_len]
        elif decoded.shape[-1] < self.seq_len:
            decoded = nn.functional.pad(decoded, (0, self.seq_len - decoded.shape[-1]))
        return decoded.transpose(1, 2)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        latent = self.encode(x)
        reconstruction = self.decode(latent)
        return reconstruction, latent


def parse_args() -> argparse.Namespace:
'''
if "class CompactTemporalConvAutoencoder(nn.Module):" not in text:
    if class_anchor not in text:
        raise SystemExit("class insert anchor not found")
    text = text.replace(class_anchor, class_insert, 1)

old_feature_arg = '    parser.add_argument("--feature-profile", choices=["full", "trimmed"], default="full")\n'
new_feature_arg = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_motion_accel_mask"], default="full")\n    parser.add_argument("--model-profile", choices=["deep4", "compact3"], default="deep4")\n'
if '"--model-profile"' not in text:
    if old_feature_arg not in text:
        raise SystemExit("feature arg anchor not found")
    text = text.replace(old_feature_arg, new_feature_arg, 1)

resolve_old = '''def resolve_feature_profile(args: argparse.Namespace) -> tuple[list[str], list[str], list[str], bool]:
    if args.feature_profile == "trimmed":
        return (
            TRIMMED_DYNAMIC_NUMERIC_COLUMNS,
            TRIMMED_DYNAMIC_BOOLEAN_COLUMNS,
            TRIMMED_DERIVED_COLUMNS,
            False,
        )
    return (
        DYNAMIC_NUMERIC_COLUMNS,
        DYNAMIC_BOOLEAN_COLUMNS,
        DERIVED_COLUMNS,
        True,
    )
'''
resolve_new = '''def resolve_feature_profile(args: argparse.Namespace) -> tuple[list[str], list[str], list[str], bool]:
    if args.feature_profile == "rgb_state_motion_accel":
        return (
            RGB_STATE_MOTION_NUMERIC_COLUMNS,
            RGB_STATE_MOTION_BOOLEAN_COLUMNS,
            RGB_STATE_MOTION_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_motion_accel_mask":
        return (
            RGB_MOTION_ACCEL_NUMERIC_COLUMNS,
            RGB_MOTION_ACCEL_BOOLEAN_COLUMNS,
            RGB_MOTION_ACCEL_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "trimmed":
        return (
            TRIMMED_DYNAMIC_NUMERIC_COLUMNS,
            TRIMMED_DYNAMIC_BOOLEAN_COLUMNS,
            TRIMMED_DERIVED_COLUMNS,
            False,
        )
    return (
        DYNAMIC_NUMERIC_COLUMNS,
        DYNAMIC_BOOLEAN_COLUMNS,
        DERIVED_COLUMNS,
        True,
    )
'''
if "rgb_state_motion_accel" not in text:
    if resolve_old not in text:
        raise SystemExit("resolve block not found")
    text = text.replace(resolve_old, resolve_new, 1)

model_old = '''    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = TemporalConvAutoencoder(
        feature_dim=prepared.sequences.shape[2],
        latent_dim=args.latent_dim,
        seq_len=args.max_seq_len,
    ).to(device)
'''
model_new = '''    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_cls = CompactTemporalConvAutoencoder if args.model_profile == "compact3" else TemporalConvAutoencoder
    model = model_cls(
        feature_dim=prepared.sequences.shape[2],
        latent_dim=args.latent_dim,
        seq_len=args.max_seq_len,
    ).to(device)
'''
if "model_cls = CompactTemporalConvAutoencoder" not in text:
    if model_old not in text:
        raise SystemExit("model block not found")
    text = text.replace(model_old, model_new, 1)

meta_old = '        "latent_dim": args.latent_dim,\n        "cluster_count_requested": args.cluster_count,\n'
meta_new = '        "latent_dim": args.latent_dim,\n        "model_profile": args.model_profile,\n        "cluster_count_requested": args.cluster_count,\n'
if '"model_profile": args.model_profile' not in text:
    if meta_old not in text:
        raise SystemExit("metadata block not found")
    text = text.replace(meta_old, meta_new, 1)

if text != original:
    path.write_text(text)
    print(f"updated {path}")
else:
    print("no changes needed")
