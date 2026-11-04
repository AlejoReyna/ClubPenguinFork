"""Offline catalog selection and playback verification; no UI events."""
import base64
import unittest

from experiments.puffle_sim.music_catalog_mac import play_catalog, select_song


RESULTS = ('Suzuki\tSong\u202f·\u202fFuerza Regida\n'
           'Suzuki - Single\tAlbum\u202f·\u202fFuerza Regida\n'
           'Another song\tSong\u202f·\u202fOtro Artista\n')


class CatalogSelectionTests(unittest.TestCase):
    def test_song_and_artist_match(self):
        self.assertEqual(select_song('suzuki de fuerza regida', RESULTS),
                         ('Suzuki', 'Song\u202f·\u202fFuerza Regida'))

    def test_album_and_unrelated_tracks_are_ignored(self):
        self.assertIsNone(select_song('canción ausente', RESULTS))
        self.assertIsNone(select_song('suzuki', 'Suzuki\tAlbum · Fuerza Regida'))

    def test_unicode_and_spanish_song_label(self):
        self.assertEqual(select_song('cancion de mar', 'Canción\tCanción · Mar'),
                         ('Canción', 'Canción · Mar'))


class CatalogPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_query_never_becomes_script_source(self):
        query = 'suzuki " end tell'
        calls = []
        async def runner(script, **kwargs):
            calls.append(script)
            return ''
        self.assertEqual(await play_catalog(query, runner), ('NOT_FOUND', ''))
        self.assertNotIn(query, calls[0])
        self.assertIn(base64.b64encode(query.encode()).decode(), calls[0])

    async def test_failed_or_unverified_playback_never_claims_success(self):
        responses = iter([RESULTS, 'UNVERIFIED', '10,20', '', 'CLICKED',
                          'UNVERIFIED', 'UNVERIFIED', 'UNVERIFIED', 'UNVERIFIED'])
        async def runner(script, **kwargs): return next(responses)
        self.assertEqual(await play_catalog('suzuki fuerza regida', runner), ('UNVERIFIED', ''))

    async def test_success_requires_playback_verification(self):
        responses = iter([RESULTS, 'UNVERIFIED', '10,20', '', 'CLICKED', 'PLAYED'])
        async def runner(script, **kwargs): return next(responses)
        self.assertEqual(await play_catalog('suzuki fuerza regida', runner), ('PLAYED', 'Suzuki'))

    async def test_permission_error_and_invalid_query_stop_before_click(self):
        calls = []
        async def runner(script, **kwargs):
            calls.append(script)
            return 'ERROR:-25211'
        self.assertEqual(await play_catalog('suzuki', runner), ('ERROR:-25211', ''))
        self.assertEqual(len(calls), 1)
        self.assertEqual(await play_catalog('suzuki\nplay', runner), ('ERROR:QUERY', ''))
        self.assertEqual(len(calls), 1)
