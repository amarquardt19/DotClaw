#!/usr/bin/env python3
from __future__ import annotations

import argparse

import train_fare_shape_autoencoder as base


def parse_args() -> argparse.Namespace:
    parser = base.parse_args()
    parser.feature_profile = 'value_delta_observed'
    return parser


if __name__ == '__main__':
    base.train(parse_args())
