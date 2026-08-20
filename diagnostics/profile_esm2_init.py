"""Bounded diagnostic: time GEPER's local ESM2 initialization only."""

import os
import sys
import time

os.environ.setdefault("GEPER_CONFIG_FILE", r"C:\Users\kisla\GEPER\geper\config.yaml")
os.environ.setdefault("USE_TF", "0")

sys.path.insert(0, r"C:\Users\kisla\GEPER\geper")

from models.esm2 import ESM2Model  # noqa: E402


started = time.perf_counter()
print("START", flush=True)
model = ESM2Model()
model.load()
print(f"LOAD_SECONDS={time.perf_counter() - started:.2f}", flush=True)
print(f"DEVICE={model.device}", flush=True)
print(f"PRECISION={model._report_precision()}", flush=True)
