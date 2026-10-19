#!/usr/bin/env python3
"""Isolated end-to-end workflow smoke check. Authored by [R2] SRVP2-CODEX."""
import argparse
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helix_memory import MemoryStore, recall, latest_events
from helix_memory.mirror import mirror_to_directory, restore_generation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-semantic', action='store_true', help='core-only (the default)')
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='hmc-smoke-') as temporary:
        root = Path(temporary)
        source = root / 'memory'
        store = MemoryStore(source)
        event = store.remember('Orion uses ultramarine. Next: review the draft.',
            area='work', actor='[R2] EXAMPLE-AGENT', task_id='orion', idempotency_key='first')
        assert recall('ultramarine', memory_dir=source, include_semantic=False)
        correction = store.remember('Orion now uses viridian.', area='work',
            actor='[R2] EXAMPLE-AGENT', task_id='orion', kind='correction',
            supersedes=[event['event_id']], idempotency_key='second')
        found = recall('Orion', memory_dir=source, include_semantic=False)
        assert correction['event_id'] in {item.id for item in found}
        assert event['event_id'] not in {item.id for item in found}
        assert Path(event['path']).exists()
        handoff = root / 'Living_Handoff.html'
        handoff.write_text('<html><body><h1>Existing board</h1></body></html>')
        store.update_resume(handoff, {'actor':'[R2] EXAMPLE-AGENT','task_id':'orion',
            'goal':'Finish Orion', 'next_step':'Review draft',
            'last_event_id':correction['event_id']}, expected_revision=0)
        restarted = MemoryStore(source)
        assert restarted.read_resume(handoff)['state']['next_step'] == 'Review draft'
        assert latest_events(memory_dir=source, task_id='orion')[0]['id'] == correction['event_id']
        mirror = mirror_to_directory({'memory': source, 'handoff.html': handoff}, root/'backup')
        restore_generation(mirror['local_generation'], root/'restored')
        assert (root/'restored/handoff.html').read_bytes() == handoff.read_bytes()
        print('Capture, warm-cache correction, restart recovery, full mirror and restore passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
