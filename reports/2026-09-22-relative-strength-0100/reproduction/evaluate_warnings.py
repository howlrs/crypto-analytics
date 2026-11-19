"""Evaluate current observed inputs against original fixed and rolling levels."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path('/home/o9oem/workspace/crypto/analytics')
sys.path.insert(0, str(ROOT))
from live_signals.early_warning import ReferenceLevels, evaluate
from live_signals.io import load_archive, sha256_file

OLD_REPORT = ROOT / 'reports/2026-09-21-btc-eth-hype-long/report.md'
OLD_SHA = 'd9835d4e53bc0fa4114320694a32421fde2b8c6af4e3cd454c4a13935e8af2be'
LEVELS = {'BTC': (81228.15, 81878.98, 82100), 'ETH': (2653.43, 2678.70, 2707.70),
          'HYPE': (92.92, 94.42, 94.57)}


def run(directory):
    if sha256_file(OLD_REPORT) != OLD_SHA:
        raise ValueError('Original report differs from fixed-reference source')
    manifest = json.loads((directory / 'sources.json').read_text())
    generated = time.time_ns() // 1000000
    decision = manifest['completed_ms']
    result = {'generated_at_ms': generated, 'as_of_ms': decision,
              'sources_sha256': sha256_file(directory / 'sources.json'),
              'archive_sha256': sha256_file(directory / 'public_snapshots.zip'),
              'engine_sha256': sha256_file(ROOT / 'live_signals/early_warning.py'),
              'fixed_reference_source_sha256': OLD_SHA, 'coins': {}}
    for coin, (support, high4, high7d) in LEVELS.items():
        spot, perp, _ = load_archive(directory / 'public_snapshots.zip', coin)
        variants = {}
        for name, high in (('original_high4', high4), ('original_high7d', high7d)):
            reference = ReferenceLevels(spot.venue, spot.symbol, support, high, 1789977521605)
            variants[name] = evaluate(spot, perp, as_of_ms=decision, generated_at_ms=generated,
                                      reference=reference, mode='observed')
        variants['rolling_context'] = evaluate(spot, perp, as_of_ms=decision,
                                                generated_at_ms=generated, mode='observed')
        result['coins'][coin] = variants
    with (directory / 'warnings.json').open('x') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    result = run(args.directory)
    for coin, data in result['coins'].items():
        print(coin, {name: {'state': x['state'], 'publishable': x['publishable_state'],
                            'flags': x['flags'], 'volume': x.get('confirmation', {}).get('volume_confirmed')}
                     for name, x in data.items()})
