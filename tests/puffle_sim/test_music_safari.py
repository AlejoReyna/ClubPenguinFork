"""Offline tests for the experimental Apple Music web backend."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from experiments.puffle_sim.music_safari import SafariMusicBackend, _js_script


class SafariMusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_requires_matching_full_track(self):
        responses = iter([
            {'status': 'searching'},
            {'status': 'ok', 'tracks': [{'title': 'Suzuki', 'artist': 'Fuerza Regida'}]},
            {'status': 'clicked'},
            {'status': 'ok', 'playing': True, 'ready': 4, 'duration': 180,
             'title': 'Suzuki', 'artist': 'Fuerza Regida'},
        ])
        async def runner(script): return json.dumps(next(responses))
        with patch('experiments.puffle_sim.music_safari.asyncio.sleep', new_callable=AsyncMock):
            text = await SafariMusicBackend(1, runner).run({'action': 'play', 'query': 'Suzuki Fuerza Regida'})
        self.assertEqual(text, 'Puse «Suzuki».')

    async def test_retry_when_safari_navigates(self):
        responses = iter([
            {'status': 'searching'}, {'status': 'error'},
            {'status': 'ok', 'tracks': [{'title': 'Suzuki', 'artist': 'Fuerza Regida'}]},
            {'status': 'clicked'},
            {'status': 'ok', 'playing': True, 'ready': 4, 'duration': 180,
             'title': 'Suzuki', 'artist': 'Fuerza Regida'},
        ])
        async def runner(script): return json.dumps(next(responses))
        with patch('experiments.puffle_sim.music_safari.asyncio.sleep', new_callable=AsyncMock):
            text = await SafariMusicBackend(1, runner).run({'action': 'play', 'query': 'Suzuki Fuerza Regida'})
        self.assertEqual(text, 'Puse «Suzuki».')

    async def test_preview_does_not_claim_full_playback(self):
        responses = iter([
            {'status': 'searching'},
            {'status': 'ok', 'tracks': [{'title': 'Suzuki', 'artist': 'Fuerza Regida'}]},
            {'status': 'clicked'},
            {'status': 'ok', 'playing': True, 'ready': 4, 'duration': 30,
             'title': 'Suzuki', 'artist': 'Fuerza Regida'},
        ])
        async def runner(script): return json.dumps(next(responses))
        with patch('experiments.puffle_sim.music_safari.asyncio.sleep', new_callable=AsyncMock):
            text = await SafariMusicBackend(1, runner).run({'action': 'play', 'query': 'Suzuki Fuerza Regida'})
        self.assertIn('fragmento', text)

    async def test_longer_itunes_preview_is_still_a_preview(self):
        responses = iter([
            {'status': 'searching'},
            {'status': 'ok', 'tracks': [{'title': 'Suzuki', 'artist': 'Fuerza Regida'}]},
            {'status': 'clicked'},
            {'status': 'ok', 'playing': True, 'ready': 4, 'duration': 90, 'preview': True,
             'title': 'Suzuki', 'artist': 'Fuerza Regida'},
        ])
        async def runner(script): return json.dumps(next(responses))
        with patch('experiments.puffle_sim.music_safari.asyncio.sleep', new_callable=AsyncMock):
            text = await SafariMusicBackend(1, runner).run({'action': 'play', 'query': 'Suzuki Fuerza Regida'})
        self.assertIn('fragmento', text)

    async def test_does_not_claim_success_for_wrong_track(self):
        responses = iter([
            {'status': 'searching'},
            {'status': 'ok', 'tracks': [{'title': 'Suzuki', 'artist': 'Fuerza Regida'}]},
            {'status': 'clicked'},
        ] + [{'status': 'ok', 'playing': True, 'ready': 4, 'duration': 180,
              'title': 'Otra', 'artist': 'Otro'}] * 12)
        async def runner(script): return json.dumps(next(responses))
        with patch('experiments.puffle_sim.music_safari.asyncio.sleep', new_callable=AsyncMock):
            text = await SafariMusicBackend(1, runner).run({'action': 'play', 'query': 'Suzuki Fuerza Regida'})
        self.assertIn('No pude confirmar', text)

    async def test_unavailable_safari_returns_actionable_message(self):
        async def runner(script): raise asyncio.TimeoutError()
        text = await SafariMusicBackend(1, runner).run({'action': 'play', 'query': 'Suzuki'})
        self.assertIn('ventana conectada', text)

    def test_untrusted_js_is_encoded_and_window_is_validated(self):
        source = '"; do shell script "bad"'
        self.assertNotIn(source, _js_script(1, source))
        with self.assertRaises(ValueError):
            _js_script('1; arbitrary', source)
