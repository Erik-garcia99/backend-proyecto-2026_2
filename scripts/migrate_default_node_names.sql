-- Asigna un nombre legible y único a los nodos que todavía no tienen nombre.
-- No toca nombres ya editados por el usuario. Repetir la migración es seguro.
BEGIN;
UPDATE public.esp32_node
SET name = (CASE node_type
              WHEN 'guide' THEN 'Guía de luces'
              WHEN 'access_counter' THEN 'Control de aforo'
              WHEN 'shelf_cam' THEN 'Cámara'
              WHEN 'environment' THEN 'Sensor ambiental'
              ELSE 'Dispositivo'
            END) || ' ' || right(mac_address, 5)
WHERE name IS NULL OR btrim(name) = '';
COMMIT;
