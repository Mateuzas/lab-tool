"""config/ranking.json (CONTRACTS §4): weights and fixed FX rates, written with defaults on first run."""

import copy
import os

from bounty.util import read_json, write_json

DEFAULT_RANKING = {
    "weights": {"payout": 0.35, "findability": 0.30, "competition": 0.25, "friction": 0.10},
    "fx": {"EUR": 1, "USD": 0.92, "GBP": 1.17},
}


def ranking_path(ws):
    return os.path.join(ws, "config", "ranking.json")


def load_ranking(ws):
    """Read config/ranking.json, writing the default if it is missing. Missing keys fall back to defaults."""
    path = ranking_path(ws)
    cfg = read_json(path)
    if cfg is None:
        cfg = copy.deepcopy(DEFAULT_RANKING)
        write_json(path, cfg)
        return cfg
    merged = copy.deepcopy(DEFAULT_RANKING)
    for key in ("weights", "fx"):
        if isinstance(cfg.get(key), dict):
            merged[key].update(cfg[key])
    return merged
