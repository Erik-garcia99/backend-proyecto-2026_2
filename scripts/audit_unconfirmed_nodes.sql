-- Registros que nunca fueron confirmados por el usuario.
-- Un nodo con node_code NULL sigue siendo candidato de búsqueda; si además
-- nunca tuvo heartbeat o lleva días sin aparecer, es un registro huérfano.
-- Revisar el resultado antes de borrar.
SELECT n.id,
       n.mac_address,
       n.name,
       n.node_code,
       n.registration_status,
       n.registered_at,
       n.last_heartbeat_at,
       n.location,
       g.rpi_identifier,
       g.branch_id
FROM public.esp32_node n
JOIN public.gateway g ON g.id = n.gateway_id
WHERE n.node_code IS NULL
ORDER BY n.registered_at;

-- Borrado de un registro concreto (revisar primero el SELECT de arriba).
-- Sustituir el UUID y la MAC; el guard node_code IS NULL evita borrar un nodo
-- que ya fue confirmado entre la revisión y el borrado.
--
-- BEGIN;
-- DELETE FROM public.esp32_node
-- WHERE id = '00000000-0000-0000-0000-000000000000'
--   AND mac_address = '00:00:00:00:00:00'
--   AND node_code IS NULL;
-- COMMIT;
