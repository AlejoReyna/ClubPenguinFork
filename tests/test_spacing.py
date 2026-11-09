"""Natural presence: personal space, talking rings, player pacing and attention."""
import asyncio
import itertools
import math
from pathlib import Path
import random
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bots import spacing
from bots.config import ConfigError, build_config
from bots.life import ATTENTIVE_REST, Life
from bots.rooms import Rect
from bots.world import World

from test_brain import FakeVoice, fixture
from test_director import drain, sent
from test_session import wait_until
from test_world import record

CARDS = Path(__file__).resolve().parents[1] / 'containers/bots/cast'
SKI_HILL = ('avalancha', 'bolazo', 'copito', 'nevado', 'ventisca')  # five residents of room 230


def bodies(usernames, personal=55.0, approximate=1, walk_weight=4, seed=7):
    """Card bodies sharing one World, joined at their posts like a fresh login."""
    config = build_config({'bots': [{'username': name, 'password': 'synthetic'} for name in usernames],
                           'service': {'life_seed': seed},
                           'behavior': {'personal_space': personal},
                           'life': {'approximate_walk': approximate, 'walk_weight': walk_weight}},
                          {'BOTS_CAST_DIR': str(CARDS)})
    world = World()
    lives = []
    for offset, bot in enumerate(config.bots):
        player_id = 500 + offset
        world.register_bot(player_id)
        session = SimpleNamespace(
            bot=bot, room_id=bot.room, player_id=player_id, world=world, service=config.service,
            catalog=config.catalog, body_lock=asyncio.Lock(), director=None, traveling=False,
            behavior=SimpleNamespace(behavior=config.behavior),
            travel=SimpleNamespace(settings=config.life, leases={}, jobs={}, reservations={},
                                   consider=AsyncMock()),
            send_frame=AsyncMock(), send_action=AsyncMock())

        async def move(x, y, own=session):
            own.world.member_moved(own.room_id, own.player_id, x, y)
        session.send_move = AsyncMock(side_effect=move)
        lives.append(Life(session))
    for room in {life.session.room_id for life in lives}:
        world.snapshot(room, [record(bot.session.player_id, bot.bot.nickname, *bot.position)
                              for bot in lives if bot.session.room_id == room])
    return config, world, lives


def stacked(lives):
    points = [life.position for life in lives]
    return sum(1 for a, b in itertools.combinations(points, 2) if math.dist(a, b) < spacing.OVERLAP_PX)


class SpacingFunctionTests(unittest.TestCase):
    def test_pick_spot_prefers_free_points_and_falls_back_to_the_roomiest(self):
        rng = random.Random(1)
        rect = Rect(0, 300, 0, 200)
        others = [(150, 100), (40, 40)]
        for _ in range(200):
            point = spacing.pick_spot([rect], others, rng, personal=55)
            self.assertTrue(rect.contains(*point))
            self.assertGreaterEqual(spacing.nearest(point, others), 55)
        tiny = Rect(100, 110, 100, 110)
        point = spacing.pick_spot([tiny], [(105, 105)], rng, personal=55)
        self.assertTrue(tiny.contains(*point))  # nowhere is free: still a trusted point, the roomiest one
        self.assertIsNone(spacing.pick_spot([], others, rng, personal=55))

    def test_pick_spot_near_an_anchor_stays_within_the_radius_and_the_rectangle(self):
        rng = random.Random(2)
        rect = Rect(0, 700, 0, 400)
        for _ in range(200):
            x, y = spacing.pick_spot([rect], [(380, 220)], rng, personal=55, near=(380, 220), radius=120)
            self.assertLessEqual(abs(x - 380), 120)
            self.assertLessEqual(abs(y - 220), 120)
        self.assertIsNone(spacing.pick_spot([Rect(0, 10, 0, 10)], [], rng, personal=55,
                                            near=(500, 500), radius=50))

    def test_ring_spot_is_at_talking_distance_free_inside_and_on_the_near_side(self):
        rng = random.Random(3)
        rect = Rect(0, 700, 0, 400)
        center, origin = (350, 200), (100, 200)
        for _ in range(100):
            point = spacing.ring_spot(center, [rect], [center, (420, 200)], rng, personal=55, origin=origin)
            self.assertTrue(rect.contains(*point))
            self.assertTrue(59 <= math.dist(point, center) <= 91)
            self.assertGreaterEqual(math.dist(point, (420, 200)), 55)
            self.assertLess(point[0], center[0])  # the bot's own side of the person
        self.assertIsNone(spacing.ring_spot(center, [Rect(340, 360, 190, 210)], [center], rng, personal=55))

    def test_overlap_pairs_counts_bot_pairs_and_bots_on_people(self):
        self.assertEqual(spacing.overlap_pairs([(0, 0), (10, 0), (200, 0)]), 1)
        self.assertEqual(spacing.overlap_pairs([(0, 0)], fixed=[(5, 5), (300, 300)]), 1)
        self.assertEqual(spacing.overlap_pairs([]), 0)


class BodySpacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_arrivals_step_off_the_shared_spawn_onto_free_trusted_spots(self):
        config, _world, lives = bodies(SKI_HILL)
        room = config.catalog.require(230)
        self.assertEqual(stacked(lives), 10)  # all five joined on the designer spawn
        for life in lives:
            async with life.session.body_lock:
                await life.settle()
        self.assertEqual(stacked(lives), 0)
        for life in lives:
            self.assertTrue(any(rect.contains(*life.position) for rect in room.approx_walk))
            self.assertLessEqual(max(abs(life.position[0] - room.spawn[0]),
                                     abs(life.position[1] - room.spawn[1])), spacing.ARRIVAL_RADIUS)

    async def test_without_trusted_floor_or_with_spacing_off_the_post_is_kept(self):
        for kwargs in ({'approximate': 0}, {'personal': 0}):
            _config, _world, lives = bodies(SKI_HILL, **kwargs)
            for life in lives:
                async with life.session.body_lock:
                    self.assertFalse(await life.settle())
                life.session.send_move.assert_not_awaited()

    async def test_steady_state_walking_keeps_penguins_apart(self):
        results = {}
        for personal in (0, 55):
            _config, _world, lives = bodies(SKI_HILL, personal=personal)
            for life in lives:
                async with life.session.body_lock:
                    await life.settle()
            rng, overlapping, pairs = random.Random(5), 0, 0
            with patch('bots.life.asyncio.sleep', new=AsyncMock()):
                for _ in range(600):
                    life = rng.choice(lives)
                    async with life.session.body_lock:
                        await life._step()
                    overlapping += stacked(lives)
                    pairs += 10
            results[personal] = overlapping / pairs
        self.assertLess(results[55], 0.02)
        self.assertGreater(results[0], results[55])

    async def test_a_body_on_a_free_spot_of_its_post_stays_and_a_crowded_one_moves(self):
        config, world, lives = bodies(('lolaritmo',))
        life = lives[0]
        floor = config.catalog.require(120).pois['dance_floor'].rect
        life.activity = 'dance'
        self.assertTrue(floor.contains(*life.position))
        self.assertIsNone(life.target())  # dancing in place, not shuffling every step
        life.position = (285, 235)
        world.member_added(120, record(999, 'Human', 282, 232))  # someone dances on top of it
        point = life.target()
        self.assertTrue(floor.contains(*point))
        self.assertGreaterEqual(math.dist(point, (282, 232)), 55)
        life.position = (285, 235)
        world.member_moved(120, 999, 315, 245)  # 32 px away: overlapping, so it must move
        point = life.target()
        self.assertTrue(floor.contains(*point))
        self.assertGreaterEqual(math.dist(point, (315, 245)), 55)
        life.position = point
        self.assertIsNone(life.target())  # no overlap any more: it stays and dances
        life.position = (330, 250)
        world.member_moved(120, 999, 330, 247)  # centre of a 100x35 floor: nothing is 55 px away
        point = life.target()
        self.assertTrue(floor.contains(*point))
        self.assertGreater(math.dist(point, (330, 247)), 45)  # still the roomiest trusted point

    async def test_active_schedule_labels_roam_and_post_labels_stay(self):
        config, _world, lives = bodies(('bolazo',))  # "play" at Ski Hill for nine hours, no post
        life = lives[0]
        rects = config.catalog.require(230).approx_walk
        life.activity = 'play'
        points = [life.target() for _ in range(200)]
        moved = [point for point in points if point is not None]
        self.assertTrue(100 < len(moved) < 190)  # mostly on the move, not every single step
        self.assertTrue(all(any(rect.contains(*point) for rect in rects) for point in moved))
        life.activity = 'work'
        self.assertTrue(all(life.target() is None for _ in range(50)))

    async def test_a_follower_or_talker_stands_beside_the_player_not_on_them(self):
        config, world, lives = bodies(('peperina',))  # pizza parlor: a large measured floor
        life = lives[0]
        walk = config.catalog.require(330).walk
        world.member_added(330, record(999, 'Human', 600, 400))
        life.session.travel.leases[life.bot.index] = SimpleNamespace(human=999)
        point = life.target()
        self.assertTrue(any(rect.contains(*point) for rect in walk))
        self.assertTrue(59 <= math.dist(point, (600, 400)) <= 91)
        life.position = point
        self.assertIsNone(life.target())  # already beside them: stays
        life.session.travel.leases.clear()
        life.session.director = SimpleNamespace(partner=lambda session: 999)
        world.member_moved(330, 999, 200, 300)
        point = life.target()
        self.assertTrue(59 <= math.dist(point, (200, 300)) <= 91)
        async with life.session.body_lock:
            self.assertTrue(await life.approach(999))
        self.assertTrue(59 <= math.dist(life.position, (200, 300)) <= 91)
        async with life.session.body_lock:
            self.assertFalse(await life.approach(999))  # already beside them


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rests_are_long_tailed_around_the_median_and_short_while_attentive(self):
        _config, _world, lives = bodies(('nevado',))
        life = lives[0]
        settings = SimpleNamespace(rest_median=35.0, life_min_delay=5.0)
        values = sorted(life._rest(settings) for _ in range(2000))
        self.assertTrue(28 <= values[1000] <= 42)
        self.assertGreaterEqual(values[0], 5.0)
        self.assertLessEqual(values[-1], 175.0)
        self.assertGreater(values[1900], 2 * values[100])  # uneven, unlike a uniform 5-14 s tick
        life.session.director = SimpleNamespace(partner=lambda session: 999)
        for _ in range(50):
            self.assertTrue(ATTENTIVE_REST[0] <= life._rest(settings) <= ATTENTIVE_REST[1])

    async def test_away_from_keyboard_ends_on_wake_and_never_while_attentive(self):
        _config, _world, lives = bodies(('nevado',))
        life = lives[0]
        task = asyncio.create_task(life._away())
        await asyncio.sleep(0)
        self.assertTrue(life.afk)
        life.wake()
        await asyncio.wait_for(task, 1)
        self.assertFalse(life.afk)
        life.session.director = SimpleNamespace(partner=lambda session: 999)
        self.assertFalse(any(life._goes_afk(SimpleNamespace(afk_chance=1.0)) for _ in range(20)))

    async def test_new_knobs_are_validated(self):
        def build(behavior=None, dialogue=None):
            return build_config({'bots': [{'username': 'nevado', 'password': 'synthetic'}],
                                 'behavior': behavior or {}, 'dialogue': dialogue or {}}, {})
        config = build()
        self.assertEqual((config.behavior.personal_space, config.behavior.rest_median), (55.0, 35.0))
        self.assertEqual(config.dialogue.typing_cps, 4.5)
        for behavior in ({'personal_space': 500}, {'rest_median': 0.5}, {'burst_chance': 2},
                         {'afk_chance': 1.5}):
            with self.assertRaises(ConfigError):
                build(behavior=behavior)
        for dialogue in ({'continuity_chance': 1.2}, {'chattiness_weight': 3}):
            with self.assertRaises(ConfigError):
                build(dialogue=dialogue)


class AttentionTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, **dialogue):
        provider = FakeVoice()
        manager = fixture(provider=provider, **dialogue)
        self.addAsyncCleanup(manager.close)
        return manager, provider, manager.director

    async def test_a_bot_already_talking_with_the_player_keeps_answering(self):
        manager, _provider, director = await self.make(other_chance=0, hello_chance=0, question_chance=0)
        self.assertIsNone(director.choose(120, 999, 'y tu que haces'))
        director.continuity[(120, 999)] = (0, director.clock())
        self.assertEqual(director.choose(120, 999, 'y tu que haces'), manager.sessions[0])
        director.settings.continuity_chance = 0
        self.assertIsNone(director.choose(120, 999, 'y tu que haces'))

    async def test_far_players_and_shy_characters_are_heard_less(self):
        manager, _provider, director = await self.make(hearing_radius=220)
        director.rng = SimpleNamespace(random=lambda: 0.5)
        self.assertEqual(director.choose(120, 999, 'que tal').bot.username, 'mambo')  # 10 px away
        manager.world.member_moved(120, 999, 760, 480)  # across the room
        self.assertIsNone(director.choose(120, 999, 'que tal'))
        self.assertEqual(director.choose(120, 999, 'Mambo, que tal').bot.username, 'mambo')  # named
        director.settings.hearing_radius, director.settings.chattiness_weight = 0, 1
        lola, tito, mambo = manager.sessions
        self.assertAlmostEqual(director._attention(120, tito, 999), 0.7)
        self.assertAlmostEqual(director._attention(120, mambo, 999), 1.3)

    async def test_typing_time_scales_with_length_and_counts_model_latency(self):
        manager, _provider, director = await self.make(typing_cps=4.5)
        session = manager.sessions[0]
        line = 'jaja si, ven a bailar'
        for _ in range(50):
            delay = director._typing(session, line)
            self.assertTrue(0.6 + len(line) / 5.4 <= delay <= 1.8 + len(line) / 3.6)
        self.assertEqual(director._typing(session, line, elapsed=30), 0)
        self.assertLessEqual(director._typing(session, 'x' * 100), 9.0)
        director.settings.typing_cps = 0
        self.assertEqual(director._typing(session, line), 0)

    async def test_replies_wait_for_typing_and_the_speaker_walks_over(self):
        manager, _provider, director = await self.make()
        typed = []
        director._typing = lambda session, line, elapsed=0.0: typed.append(line) or 0.0
        lola = manager.sessions[0]
        lola.behavior.life = SimpleNamespace(afk=False, activity='idle', approach=AsyncMock())
        await director.on_chat(lola, 999, 'Lola, hola')
        await drain(director)
        self.assertEqual(typed, [sent(manager)[0][1]])
        lola.behavior.life.approach.assert_awaited_once_with(999)
        self.assertIs(director.partner(lola), 999)
        self.assertIsNone(director.partner(manager.sessions[1]))
        now = director.clock()
        director.clock = lambda: now + 61
        self.assertIsNone(director.partner(lola))

    async def test_afk_bots_skip_unnamed_chat_and_wake_when_named(self):
        manager, _provider, director = await self.make()
        mambo = manager.sessions[2]
        mambo.behavior.life = SimpleNamespace(afk=True, activity='idle', wake=Mock(), approach=AsyncMock())
        self.assertEqual(director.choose(120, 999, 'hola').bot.username, 'titogroove')  # next nearest
        await director.on_chat(manager.sessions[0], 999, 'Mambo, sigues ahi?')
        await drain(director)
        mambo.behavior.life.wake.assert_called_once()
        self.assertEqual(sent(manager)[-1][0], 'mambo')

    async def test_the_nearest_bot_greets_a_newcomer(self):
        manager, _provider, director = await self.make(greeting_chance=1, greeting_delay_min=0,
                                                       greeting_delay_max=0)
        manager.world.member_added(120, record(998, 'Nuevo', 302, 251))
        director.member_added(120, 998)
        await wait_until(lambda: len(sent(manager)) == 1)
        self.assertEqual(sent(manager)[0][0], 'lolaritmo')  # standing at x=300


class SharedWorldTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_move_claims_its_destination_and_the_gauge_counts_overlaps(self):
        manager = fixture()
        self.addAsyncCleanup(manager.close)
        lola = manager.sessions[0]
        lola.conn.send_move = AsyncMock()
        await lola.send_move(600, 400)
        member = next(m for m in manager.world.members(120) if m.id == lola.player_id)
        self.assertEqual((member.x, member.y), (600, 400))
        text = manager.registry.render()
        # Lola walked away; Tito (320) and Mambo (340) are 20 px apart and 30/10 px from the player at 350.
        self.assertIn('bots_overlapping_pairs{room="120"} 3', text)


if __name__ == '__main__':
    unittest.main()
