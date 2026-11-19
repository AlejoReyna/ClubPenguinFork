-- Five disco party guests (bikini + wig). Run with bot sessions stopped.
BEGIN;
LOCK TABLE penguin IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE
    names text[] := ARRAY['fiestalila','fiestasol','fiestamar','fiestaverde','fiestaglam'];
    missing int;
BEGIN
    SELECT count(*) INTO missing FROM penguin WHERE lower(username) = ANY(names);
    IF missing <> 5 THEN
        RAISE EXCEPTION 'Register the five party guest accounts first (scripts/prepare-party-guests.py)';
    END IF;
    IF (SELECT count(*) FROM item WHERE (id, type) IN
        ((270,5),(271,5),(273,5),(830,5),(4097,5),(650,2),(652,2),(656,2),(657,2),(1544,2))) <> 10 THEN
        RAISE EXCEPTION 'Required bikini / wig items missing from catalog';
    END IF;
END $$;

INSERT INTO penguin_item (penguin_id, item_id)
SELECT p.id, outfit.item_id
FROM penguin AS p
JOIN (VALUES
    ('fiestalila', 270), ('fiestalila', 657),
    ('fiestasol', 271), ('fiestasol', 652),
    ('fiestamar', 273), ('fiestamar', 656),
    ('fiestaverde', 830), ('fiestaverde', 650),
    ('fiestaglam', 4097), ('fiestaglam', 1544))
     AS outfit(username, item_id) ON lower(p.username) = outfit.username
ON CONFLICT (penguin_id, item_id) DO NOTHING;

UPDATE penguin SET
    nickname = CASE lower(username)
        WHEN 'fiestalila' THEN 'Lila'
        WHEN 'fiestasol' THEN 'Sol'
        WHEN 'fiestamar' THEN 'Mar'
        WHEN 'fiestaverde' THEN 'Vera'
        WHEN 'fiestaglam' THEN 'Glam' END,
    head = CASE lower(username)
        WHEN 'fiestalila' THEN 657 WHEN 'fiestasol' THEN 652 WHEN 'fiestamar' THEN 656
        WHEN 'fiestaverde' THEN 650 WHEN 'fiestaglam' THEN 1544 END,
    body = CASE lower(username)
        WHEN 'fiestalila' THEN 270 WHEN 'fiestasol' THEN 271 WHEN 'fiestamar' THEN 273
        WHEN 'fiestaverde' THEN 830 WHEN 'fiestaglam' THEN 4097 END
WHERE lower(username) IN ('fiestalila','fiestasol','fiestamar','fiestaverde','fiestaglam');
COMMIT;