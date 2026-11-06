-- SelfIQ — Fase 3
-- Localiza la cámara shelf_cam y su sucursal para llenar seed_fase3_roi.sql.
--
-- Uso: Supabase → SQL Editor → pegar y ejecutar.
-- Toma de la primera fila: camera_node_id y branch_id.

-- 1) Cámaras registradas como shelf_cam (las más recientes/encendidas primero).
SELECT n.id            AS camera_node_id,
       n.name          AS camera_name,
       n.node_code,
       n.mac_address,
       n.is_online,
       n.last_heartbeat_at,
       n.registration_status,
       g.id            AS gateway_id,
       g.display_name  AS gateway_name,
       g.is_online     AS gateway_online,
       b.id            AS branch_id,
       b.name          AS branch_name
FROM public.esp32_node n
JOIN public.gateway g ON g.id = n.gateway_id
JOIN public.branch b ON b.id = g.branch_id
WHERE n.node_type = 'shelf_cam'
ORDER BY n.is_online DESC, n.last_heartbeat_at DESC NULLS LAST, n.created_at DESC;

-- 2) Si la consulta anterior no devuelve filas, revisa todos los nodos
--    y su tipo real. Puede que la cámara esté registrada con otro node_type.
SELECT n.id            AS node_id,
       n.node_type,
       n.name,
       n.mac_address,
       n.is_online,
       n.last_heartbeat_at,
       b.id            AS branch_id,
       b.name          AS branch_name
FROM public.esp32_node n
JOIN public.gateway g ON g.id = n.gateway_id
JOIN public.branch b ON b.id = g.branch_id
ORDER BY n.node_type, n.last_heartbeat_at DESC NULLS LAST;
