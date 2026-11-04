-- Reviewable migration for the three existing work accounts. Run only while
-- all bot sessions are stopped, after saving the current rows and Secret.
-- Item IDs are present in the bundled Houdini catalog and legacy SWF assets.
BEGIN;
LOCK TABLE penguin IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF (SELECT count(*) FROM penguin
        WHERE username IN ('baristaia', 'djia', 'pizzeroia')
          AND active AND approval_es AND NOT moderator AND NOT stealth_moderator) <> 3 THEN
        RAISE EXCEPTION 'Expected three active, Spanish-approved, non-moderator accounts';
    END IF;
    IF EXISTS (SELECT 1 FROM penguin WHERE
               (username = 'baristaia' AND nickname NOT IN ('BaristaIA', 'Juanito')) OR
               (username = 'djia' AND nickname NOT IN ('DJIA', 'DJ Cobra')) OR
               (username = 'pizzeroia' AND nickname NOT IN ('PizzeroIA', 'Viterbos'))) THEN
        RAISE EXCEPTION 'A work account has an unexpected nickname';
    END IF;
    IF EXISTS (SELECT 1 FROM penguin
               WHERE username NOT IN ('baristaia', 'djia', 'pizzeroia')
                 AND lower(nickname) IN ('juanito', 'dj cobra', 'viterbos')) THEN
        RAISE EXCEPTION 'A requested nickname already belongs to another account';
    END IF;
    IF (SELECT count(*) FROM item WHERE (id, type) IN ((262, 5), (263, 5), (424, 2), (481, 2))) <> 4 THEN
        RAISE EXCEPTION 'Required clothing items or types are missing';
    END IF;
END $$;

INSERT INTO penguin_item (penguin_id, item_id)
SELECT p.id, outfit.item_id
FROM penguin AS p
JOIN (VALUES ('baristaia', 262), ('djia', 481), ('pizzeroia', 424), ('pizzeroia', 263))
     AS outfit(username, item_id) ON outfit.username = p.username
ON CONFLICT (penguin_id, item_id) DO NOTHING;

UPDATE penguin SET
    nickname = CASE username WHEN 'baristaia' THEN 'Juanito'
                            WHEN 'djia' THEN 'DJ Cobra'
                            WHEN 'pizzeroia' THEN 'Viterbos' END,
    head = CASE username WHEN 'djia' THEN 481 WHEN 'pizzeroia' THEN 424 ELSE head END,
    body = CASE username WHEN 'baristaia' THEN 262 WHEN 'pizzeroia' THEN 263 ELSE body END
WHERE username IN ('baristaia', 'djia', 'pizzeroia');
COMMIT;
