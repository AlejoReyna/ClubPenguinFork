#!/usr/bin/env python3
"""Append five party guest bots to work-credentials.json (passwords preserved)."""
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = ROOT / '.local/bots/work-credentials.json'


def party_bots():
    # Spread homes so catalog bot_cap per room stays valid until travel.
    specs = (
        ('fiestalila', 'Lila', 121),
        ('fiestasol', 'Sol', 121),
        ('fiestamar', 'Mar', 801),
        ('fiestaverde', 'Vera', 802),
        ('fiestaglam', 'Glam', 802),
    )
    bots = []
    for username, nickname, room in specs:
        bots.append({
            'username': username,
            'password': secrets.token_urlsafe(16),
            'nickname': nickname,
            'aliases': [nickname],
            'personality': 'Invitada de la fiesta dev; solo habla en escenas /party.',
            'room': room,
            'stationary': True,
            'anchor': {'x': 380, 'y': 330},
            'party_guest': True,
        })
    return bots


def main():
    if not CREDENTIALS.is_file():
        raise SystemExit(f'Missing {CREDENTIALS} — run prepare-work-bots.py first')
    doc = json.loads(CREDENTIALS.read_text())
    existing = {b['username'].lower() for b in doc.get('bots', [])}
    passwords = {b['username'].lower(): b['password'] for b in doc['bots']}
    for bot in party_bots():
        key = bot['username'].lower()
        if key in passwords:
            bot['password'] = passwords[key]
        if key not in existing:
            doc.setdefault('bots', []).append(bot)
    CREDENTIALS.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n')
    print(f'Updated {CREDENTIALS} with party guests (total {len(doc["bots"])} bots).')
    print('Register new accounts: python3 scripts/provision-glaciar-dev-bots.py')
    print('Then SQL: docker exec -i glaciar-dev-postgres psql -U houdini -d houdini -f - < scripts/party-guests.sql')


if __name__ == '__main__':
    main()