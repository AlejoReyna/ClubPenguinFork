"""Closed vocabulary of body actions and activities that cards may name.

A card never carries wire numbers. It names an action from this list and the
code maps it to one packet, so a typo or a model-written value can never turn
into an arbitrary ``u#sa``/``u#sf`` number.

The wire numbers come from the vendored client, not from memory. They were
read in ``interface.swf`` (decompiled with FFDec 26.4.0, ACTION_MENU):
  dance_btn -> sendPlayerFrame(26)   -> ``u#sf`` frame 26
  wave_btn  -> sendPlayerAction(25)  -> ``u#sa`` action 25
  sit1_btn  -> sendPlayerFrame(18)   -> ``u#sf`` frame 18 (20, 22, 24 sit in
                                        the other three directions)
Houdini rebroadcasts both and keeps the frame, so a penguin that arrives later
sees the dance. Moving (``u#sp``) resets the frame, hence an activity is
re-applied after each walk. The older bots use ``u#sa`` 11 for their dance
(``bot_accounts.DANCE_ACTION_ID``); that is a different packet and is left as is.
"""

KIND_FRAME = 'frame'    # u#sf <n>  (cooldown 0.5 s on the server)
KIND_ACTION = 'action'  # u#sa <n>  (cooldown 1 s on the server)

# name -> (packet kind, number). Only verified numbers belong here.
BODY_ACTIONS = {
    'dance': (KIND_FRAME, 26),
    'sit': (KIND_FRAME, 18),
    'wave': (KIND_ACTION, 25),
}

# Activities a schedule block may name. Those in BODY_ACTIVITY also move the
# body; the rest are narrative labels (used by the prompts of later phases and
# by the optional local travel controller).
BODY_ACTIVITY = {'dance': 'dance', 'sit': 'sit', 'rest': 'sit', 'wave': 'wave'}
LABEL_ACTIVITIES = frozenset({
    'idle', 'eat', 'perform', 'train', 'watch', 'work', 'play', 'sled', 'fish', 'patrol',
})
ACTIVITIES = frozenset(BODY_ACTIVITY) | LABEL_ACTIVITIES

# Points of interest a card may name as a home. A measured room defines the
# ones it really has; an unmeasured room has none (the bot holds one post).
KNOWN_POIS = frozenset({
    'dance_floor', 'booth', 'arcade', 'stage', 'mat', 'sled_start', 'pier', 'counter', 'window',
})
