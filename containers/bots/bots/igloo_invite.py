"""Who may walk to an igloo, and why. The model never decides.

Rules are the closed ``reason`` vocabulary in ``dialog_log`` (docs/bots.md).
A seeded roll is ``seeded_roll``. ``helper`` and ``guide`` never get that far:
they are skipped before an invitation exists, so there is no decline event
(the vocabulary has no role code, and ``dialog_log.py`` is not ours to extend).
"""
import random
import zlib

from .cast import may_enter_igloo

# A relationship of one of these kinds does not raise the chance of saying yes.
COLD_KINDS = frozenset({
    'rival_amistosa', 'rival_amistoso', 'evitado_por', 'no_soporta_a',
    'discute_con', 'regana_a', 'reganado_por',
})
HOST_CHANCE = 0.05
PAIR_SECONDS = 7200.0


def decision_rng(life_seed, username, day, host_key):
    """Same seed, day and pair: same roll. Independent of how many steps ran."""
    material = f'{day}:{host_key}'.encode()
    return random.Random(int(life_seed) ^ zlib.crc32(str(username).encode()) ^ zlib.crc32(material))


def on_duty(card, now):
    """A ``work`` block covering this minute at the home room. The body stays."""
    if card is None or now is None:
        return False
    minute = now.hour * 60 + now.minute
    return any(block.activity == 'work' and block.room == card.home_room and block.covers(minute)
               for block in card.schedule)


def warm_toward(card, names):
    """True when the card names ``names`` in a relationship that is not cold."""
    if card is None:
        return False
    from . import text
    cold = warm = False
    for row in card.relationships:
        if not text.mentions(row['with'], names):
            continue
        if row['kind'] in COLD_KINDS:
            cold = True
        else:
            warm = True
    return warm and not cold


def verdict(rng, *, role_blocked, on_duty, pinned, busy, energy, social, recent, warm, host_is_human):
    """``(decision, reason)``. ``decision`` is accept, decline or skip.

    ``skip`` is only the role gate: no invitation is born. Every other
    outcome carries a reason from the dialog-log vocabulary.
    """
    if role_blocked:
        return ('skip', None)
    if on_duty or pinned:
        return ('decline', 'off_schedule')
    if busy:
        return ('decline', 'busy_with_human')
    if energy < 0.2:
        return ('decline', 'low_energy')
    if recent:
        return ('decline', 'recent_decline')
    chance = 0.70 if host_is_human else 0.35
    if warm:
        chance += 0.20
    if energy < 0.45:
        chance -= 0.15
    if social > 0.6:
        chance += 0.10
    chance = min(0.95, max(0.05, chance))
    if rng.random() < chance:
        return ('accept', 'seeded_roll')
    return ('decline', 'seeded_roll')


def eligible(bot):
    return may_enter_igloo(bot)
