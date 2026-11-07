"""Offline Music app tests; no Apple events or music changes are sent."""
import base64
import unittest

from experiments.puffle_sim.music_mac import MusicMacBackend, make_script
from experiments.puffle_sim.spotify import SpotifyAgent


class MusicScriptTests(unittest.TestCase):
    def test_untrusted_query_is_a_string_not_applescript(self):
        query = 'Tema "especial" \\ otro'
        script = make_script({'action': 'play', 'query': query})
        self.assertIn('set encoded to "' + base64.b64encode(query.encode()).decode() + '"', script)
        self.assertNotIn(query, script)
        with self.assertRaises(ValueError):
            make_script({'action': 'play', 'query': 'uno\nend tell'})
        with self.assertRaises(ValueError):
            make_script({'action': 'execute', 'query': ''})


class MusicBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_play_uses_actual_track_name(self):
        async def catalog(query):
            self.assertEqual(query, 'Despacito')
            return 'PLAYED', 'Despacito (Remix)'
        backend = MusicMacBackend(catalog_player=catalog)
        text = await backend.run({'action': 'play', 'query': 'Despacito'})
        self.assertEqual(text, 'Puse «Despacito (Remix)».')

    async def test_does_not_claim_unverified_change(self):
        async def runner(_): return 0, 'UNVERIFIED'
        result = await MusicMacBackend(runner).run({'action': 'next', 'query': ''})
        self.assertIn('sin confirmar', result)

    async def test_permissions_not_found_and_errors_are_clear(self):
        for status, expected in [('ERROR:-1743', 'Autoriza'), ('NOT_FOUND', 'Apple Music'),
                                 ('ERROR:-10000', 'No pude')]:
            async def catalog(_, result=status): return result, ''
            actual = await MusicMacBackend(catalog_player=catalog).run({'action': 'play', 'query': 'tema'})
            self.assertIn(expected, actual)
        for status, expected in [('NO_TRACK', 'Reproduce'), ('ERROR:-10000', 'No pude')]:
            async def runner(_, result=status): return 0, result
            actual = await MusicMacBackend(runner).run({'action': 'next', 'query': ''})
            self.assertIn(expected, actual)

    async def test_owner_command_executes_without_llm_and_cancelled_session_does_not(self):
        class Inner:
            async def evaluate(self, context):
                raise AssertionError('Music commands must not invoke the model')
        calls = []
        async def runner(script):
            calls.append(script)
            return 0, 'NEXT'
        backend = MusicMacBackend(runner)
        agent = SpotifyAgent(Inner(), lambda key: backend if key == 'owner' else None,
                             min_interval=0)
        context = (('player', 'siguiente canción'),)
        self.assertEqual(await agent.evaluate_for('owner', context, active=lambda: False), (False, ''))
        self.assertEqual(calls, [])
        self.assertEqual(await agent.evaluate_for('owner', context), (True, 'Listo, siguiente canción.'))
        self.assertEqual(len(calls), 1)

    async def test_wrong_account_never_reaches_mac(self):
        class Inner:
            async def evaluate(self, context): return True, 'hola'
        calls = []
        async def runner(script):
            calls.append(script)
            return 0, 'NEXT'
        backend = MusicMacBackend(runner)
        agent = SpotifyAgent(Inner(), lambda key: backend if key == 'owner' else None, min_interval=0)
        await agent.evaluate_for('visitor', (('player', 'siguiente canción'),))
        self.assertFalse(calls)
