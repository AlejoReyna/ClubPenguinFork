"""Server-to-client puffle packet fields; performs no authorization itself.

Houdini must revalidate the room, puffle, session and recipients immediately
before delivering these fields. This function does not do that or send data.
"""
from experiments.puffle_sim.engine import speech_text


def puffle_talk_fields(room_id, puffle_id, message):
    if type(room_id) is not int or room_id <= 0 or type(puffle_id) is not int or puffle_id <= 0:
        raise ValueError('invalid room or puffle')
    text = speech_text(message)
    if not text:
        raise ValueError('empty text')
    return ('puffletalk', room_id, puffle_id, text)
