-- Solo desarrollo local. SUDO: sudo@selfiq.local / Sudo123!
-- Admin de la sucursal: admin@selfiq.local / Admin123!
BEGIN;
INSERT INTO organization(id,name,slug)
VALUES('10000000-0000-0000-0000-000000000001','SelfIQ Demo','selfiq-demo');
INSERT INTO branch(id,organization_id,name,branch_code,address)
VALUES('20000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000001','Tienda prueba','PRUEBA','Dirección de prueba');
INSERT INTO "user"(organization_id,branch_id,email,first_name,last_name,role,account_status,password_hash)
VALUES
('10000000-0000-0000-0000-000000000001',NULL,'sudo@selfiq.local','Sudo','Administrator','sudo','active',crypt('Sudo123!',gen_salt('bf'))),
('10000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','admin@selfiq.local','Admin','Tienda','manager','active',crypt('Admin123!',gen_salt('bf')));
INSERT INTO gateway(branch_id,rpi_identifier)
VALUES('20000000-0000-0000-0000-000000000001','rpi-local-01');
-- Emitir los tokens desde los endpoints autenticados: no hay secretos de tokens en el seed.
COMMIT;