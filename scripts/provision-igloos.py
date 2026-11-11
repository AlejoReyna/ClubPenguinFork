#!/usr/bin/env python3
"""Print idempotent igloo SQL for bot accounts. Never connects to a database.

The statements match the schema in the pinned Houdini ``houdini.sql``. Run them
by hand, with the bots Deployment stopped, against the lab database. This
script has no ``--execute``: it only writes SQL to stdout.

    python3 scripts/provision-igloos.py --cast containers/bots/cast
    python3 scripts/provision-igloos.py --cast containers/bots/cast --rollback
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/bots'))

from bots.cast import load_cards  # noqa: E402

_USER = re.compile(r'^[a-z0-9]{4,12}$')


def usernames_from_cast(path):
    """Every card username, including role notes. Rejects anything else."""
    names = []
    for card in load_cards(path).values():
        if not _USER.fullmatch(card.username):
            raise ValueError(f'username {card.username!r} cannot go into SQL')
        names.append(card.username)
    if not names:
        raise ValueError('no usernames')
    return tuple(sorted(set(names)))


def _in_list(names):
    return ', '.join("'" + name + "'" for name in names)


def provision_sql(names):
    """Insert a basic igloo where ``penguin.igloo`` is null, and remember the before-image.

    A second run inserts nothing: ``penguin.igloo`` is already set, and the
    audit row conflicts. Locked stays true; ``g#or`` is what opens the map.
    """
    names = tuple(names)
    if not names or any(not _USER.fullmatch(name) for name in names):
        raise ValueError('usernames must be 4-12 lowercase letters or digits')
    listed = _in_list(names)
    return f"""BEGIN;

CREATE TABLE IF NOT EXISTS igloo_provision_audit (
  penguin_id INTEGER PRIMARY KEY REFERENCES penguin(id),
  previous_igloo INTEGER,
  inserted_room_id INTEGER,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

WITH targets AS (
  SELECT id, igloo AS previous_igloo
  FROM penguin
  WHERE username IN ({listed})
    AND active
    AND NOT moderator
    AND NOT stealth_moderator
),
inserted AS (
  INSERT INTO penguin_igloo_room (penguin_id, type, flooring, location, music, locked)
  SELECT t.id, 1, 0, 1, 0, true
  FROM targets t
  WHERE t.previous_igloo IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM penguin_igloo_room r WHERE r.penguin_id = t.id
    )
  RETURNING id AS room_id, penguin_id
),
audit AS (
  INSERT INTO igloo_provision_audit (penguin_id, previous_igloo, inserted_room_id)
  SELECT t.id, t.previous_igloo, i.room_id
  FROM targets t
  LEFT JOIN inserted i ON i.penguin_id = t.id
  ON CONFLICT (penguin_id) DO NOTHING
  RETURNING penguin_id
)
UPDATE penguin p
SET igloo = COALESCE(
  p.igloo,
  (SELECT i.room_id FROM inserted i WHERE i.penguin_id = p.id),
  (SELECT min(r.id) FROM penguin_igloo_room r WHERE r.penguin_id = p.id)
)
FROM targets t
WHERE p.id = t.id
  AND p.igloo IS NULL;

COMMIT;
"""


def rollback_sql():
    """Undo only rows this provision recorded. Inventory from login stays."""
    return """BEGIN;

UPDATE penguin p
SET igloo = a.previous_igloo
FROM igloo_provision_audit a
WHERE p.id = a.penguin_id
  AND a.previous_igloo IS NULL
  AND p.igloo IS NOT DISTINCT FROM COALESCE(
    a.inserted_room_id,
    (SELECT min(r.id) FROM penguin_igloo_room r WHERE r.penguin_id = p.id)
  );

DELETE FROM penguin_igloo_room r
USING igloo_provision_audit a
WHERE r.id = a.inserted_room_id
  AND a.inserted_room_id IS NOT NULL;

DELETE FROM igloo_provision_audit;

COMMIT;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cast', default=str(ROOT / 'containers/bots/cast'),
                        help='persona directory; every username there is included')
    parser.add_argument('--rollback', action='store_true', help='print the undo script instead')
    args = parser.parse_args()
    if args.rollback:
        sys.stdout.write(rollback_sql())
        return
    sys.stdout.write(provision_sql(usernames_from_cast(args.cast)))


if __name__ == '__main__':
    main()
